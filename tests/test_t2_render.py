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


def test_render_video(synth_project):
    result = render.run(synth_project, video=True)
    assert len(result["video"]) == 4
    for path in result["video"]:
        assert _duration(Path(path)) == \
            __import__("pytest").approx(3.0, abs=0.3)


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
