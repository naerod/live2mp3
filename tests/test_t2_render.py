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
