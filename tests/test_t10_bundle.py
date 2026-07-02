"""T10 — Bundle : ZIP complet (audio, vidéo, PDF, image disque, manifest)."""
import zipfile
from pathlib import Path

from backend.pipeline import artwork, bundle, disc, render


def test_bundle_complete(synth_project):
    render.run(synth_project, video=True)
    artwork.run(synth_project)
    disc.run(synth_project, media="audio")
    out = bundle.run(synth_project)

    zpath = Path(out["bundle"])
    assert zpath.exists()
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()

    assert "manifest.yaml" in names
    assert any(n.endswith(".mp3") for n in names)
    assert any(n.endswith(".mp4") for n in names)
    assert any(n.endswith(".pdf") for n in names)
    assert any(n.endswith(".iso") for n in names)
    # front_insert + tray_card
    assert sum(1 for n in names if n.endswith(".pdf")) >= 2


def test_bundle_audio_cd_includes_cue(synth_audio_only):
    render.run(synth_audio_only, video=False)
    disc.run(synth_audio_only, gap_seconds=2.0)
    out = bundle.run(synth_audio_only)
    with zipfile.ZipFile(out["bundle"]) as z:
        names = z.namelist()
    assert any(n.endswith(".cue") for n in names)
    assert any(n.endswith(".wav") for n in names)


def test_bundle_count(synth_audio_only):
    render.run(synth_audio_only, video=False)
    out = bundle.run(synth_audio_only)
    # 4 mp3 + manifest au minimum
    assert out["count"] >= 5
