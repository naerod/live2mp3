"""Stage 4 — Render clips.

Boucle sur les pistes du manifest et coupe :
- Audio : depuis master.wav -> MP3 VBR (`libmp3lame -q:a 0`) dans build/audio/
- Vidéo : depuis master.mkv -> MP4 (`libx264 -crf 18`, coupe frame-accurate)
  dans build/video/

Chaque piste dont start/end est renseigné est rendue ; les pistes sans
timecode sont ignorées. Stage idempotent (skip si le fichier existe déjà
et --force absent).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..manifest import Manifest


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def render_audio(master_wav: Path, start: float, end: float, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start
    _run([
        "ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
        "-i", str(master_wav),
        "-c:a", "libmp3lame", "-q:a", "0",
        str(out),
    ])


def render_video(master_mkv: Path, start: float, end: float, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start
    # Seek d'entrée avant -i (rapide) + re-encode pour coupe frame-accurate.
    _run([
        "ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(master_mkv),
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-c:a", "aac", "-b:a", "256k",
        str(out),
    ])


def run(project_dir: str | Path, force: bool = False, video: bool = True) -> dict:
    project_dir = Path(project_dir)
    m = Manifest.load(project_dir / "manifest.yaml")
    master_wav = project_dir / m.data["source"]["master_wav"]
    master_mkv = project_dir / m.data["source"]["master_mkv"]
    audio_dir = project_dir / "build" / "audio"
    video_dir = project_dir / "build" / "video"

    rendered = {"audio": [], "video": []}
    for track in m.tracks:
        start, end = track.get("start"), track.get("end")
        if start is None or end is None:
            continue
        # Audio
        a_out = audio_dir / m.track_filename(track, "mp3")
        if force or not a_out.exists():
            render_audio(master_wav, float(start), float(end), a_out)
        rendered["audio"].append(str(a_out))
        # Vidéo (optionnelle : master.mkv peut être absent en test audio-only)
        if video and master_mkv.exists():
            v_out = video_dir / m.track_filename(track, "mp4")
            if force or not v_out.exists():
                render_video(master_mkv, float(start), float(end), v_out)
            rendered["video"].append(str(v_out))

    m.set_state("render", "done")
    return rendered


if __name__ == "__main__":
    import sys

    run(sys.argv[1], force="--force" in sys.argv)
