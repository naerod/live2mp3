"""T2 — Render : coupes ffmpeg exactes (durées MP3/MP4 vs manifest)."""
import subprocess
from pathlib import Path

from backend.pipeline import render


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def test_render_audio_durations(synth_audio_only):
    result = render.run(synth_audio_only, video=False)
    assert len(result["audio"]) == 4
    for path in result["audio"]:
        assert _duration(Path(path)) == \
            __import__("pytest").approx(3.0, abs=0.15)


def test_render_produces_mp3(synth_audio_only):
    render.run(synth_audio_only, video=False)
    mp3s = list((synth_audio_only / "build" / "audio").glob("*.mp3"))
    assert len(mp3s) == 4


def test_render_video_single_full_file(synth_project):
    """Le rendu vidéo produit désormais UN SEUL fichier couvrant tout le
    concert (du début de la 1re piste à la fin de la dernière), et non plus
    un clip par piste — décision du 2026-08-02 (lecture Jellyfin pénible en
    clips séparés)."""
    result = render.run(synth_project, video=True)
    assert len(result["video"]) == 1
    video_files = list((synth_project / "build" / "video-full").glob("*.mp4"))
    assert len(video_files) == 1
    assert _duration(video_files[0]) == \
        __import__("pytest").approx(12.0, abs=0.3)


def test_render_video_filename_follows_jellyfin_stem(synth_project):
    from backend.manifest import Manifest
    from backend.pipeline.render import video_filename

    render.run(synth_project, video=True)
    m = Manifest.load(synth_project / "manifest.yaml")
    expected = video_filename(m, synth_project.name)
    assert (synth_project / "build" / "video-full" / expected).exists()


def test_render_state_done(synth_audio_only):
    render.run(synth_audio_only, video=False)
    from backend.manifest import Manifest
    m = Manifest.load(synth_audio_only / "manifest.yaml")
    assert m.state("render") == "done"


def test_render_idempotent(synth_audio_only):
    render.run(synth_audio_only, video=False)
    mp3 = next((synth_audio_only / "build" / "audio").glob("*.mp3"))
    mtime = mp3.stat().st_mtime
    render.run(synth_audio_only, video=False)  # sans --force -> skip
    assert mp3.stat().st_mtime == mtime


def test_render_video_idempotent(synth_project):
    render.run(synth_project, video=True)
    v = next((synth_project / "build" / "video-full").glob("*.mp4"))
    mtime = v.stat().st_mtime
    render.run(synth_project, video=True)  # sans --force -> skip
    assert v.stat().st_mtime == mtime


def test_render_force_reencodes_existing_file(synth_audio_only):
    """Ré-éditer un album déjà rendu (même titre, timecode différent) doit
    réellement changer l'audio produit — sans --force le nom de fichier ne
    change pas et le pipeline idempotent ignorerait le nouveau découpage."""
    render.run(synth_audio_only, video=False)
    audio_dir = synth_audio_only / "build" / "audio"
    mp3 = next(audio_dir.glob("01.*"))
    mtime = mp3.stat().st_mtime

    from backend.manifest import Manifest
    m = Manifest.load(synth_audio_only / "manifest.yaml")
    m.tracks[0]["end"] = m.tracks[0]["start"] + 1.0  # découpe raccourcie
    m.save()

    render.run(synth_audio_only, video=False, force=True)
    assert mp3.stat().st_mtime > mtime
    assert _duration(mp3) == __import__("pytest").approx(1.0, abs=0.15)


def test_render_purges_orphan_files(synth_audio_only):
    """Une piste renommée ou supprimée depuis le dernier rendu ne doit pas
    laisser de fichier fantôme dans build/audio (ZIP + Jellyfin en dépendent)."""
    render.run(synth_audio_only, video=False)
    audio_dir = synth_audio_only / "build" / "audio"
    assert len(list(audio_dir.glob("*.mp3"))) == 4

    from backend.manifest import Manifest
    m = Manifest.load(synth_audio_only / "manifest.yaml")
    m.data["tracks"] = m.data["tracks"][:2]  # 2 pistes supprimées
    m.save()

    render.run(synth_audio_only, video=False, force=True)
    remaining = list(audio_dir.glob("*.mp3"))
    assert len(remaining) == 2


def test_render_video_renames_on_album_metadata_change(synth_project):
    """Un renommage d'artiste/titre change le nom du MP4 complet (suit
    download_stem) : l'ancien fichier doit être purgé, pas laissé en orphelin."""
    render.run(synth_project, video=True)
    video_dir = synth_project / "build" / "video-full"
    old_name = next(video_dir.glob("*.mp4")).name

    from backend.manifest import Manifest
    m = Manifest.load(synth_project / "manifest.yaml")
    m.data["album"]["title"] = "Nouveau Titre"
    m.save()

    render.run(synth_project, video=True, force=True)
    files = list(video_dir.glob("*.mp4"))
    assert len(files) == 1
    assert files[0].name != old_name
    assert not (video_dir / old_name).exists()
