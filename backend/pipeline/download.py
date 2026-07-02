"""Stage 1 — Download.

yt-dlp télécharge la meilleure qualité -> source/master.mkv, puis extraction
audio lossless -> source/master.wav (pas de ré-encodage lossy intermédiaire).

IMPORTANT : n'est appelé qu'avec une URL explicitement fournie par
l'utilisateur (point d'arrêt D). Ne télécharge jamais de sa propre initiative.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..manifest import Manifest


def download_master(url: str, out_mkv: Path, cookies: str | None = None) -> Path:
    out_mkv.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["yt-dlp", "-f", "bv*+ba/best", "--merge-output-format", "mkv",
           "-o", str(out_mkv)]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)
    subprocess.run(cmd, check=True)
    return out_mkv


def extract_wav(master_mkv: Path, out_wav: Path) -> Path:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-i", str(master_mkv),
        "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
        str(out_wav),
    ], check=True, capture_output=True)
    return out_wav


def run(project_dir: str | Path) -> dict:
    project_dir = Path(project_dir)
    m = Manifest.load(project_dir / "manifest.yaml")
    url = m.data.get("source", {}).get("url")
    if not url:
        raise ValueError("Aucune URL source dans le manifest (point d'arrêt D).")
    cookies = os.environ.get("YTDLP_COOKIES")
    mkv = download_master(url, project_dir / m.data["source"]["master_mkv"], cookies)
    wav = extract_wav(mkv, project_dir / m.data["source"]["master_wav"])
    m.set_state("download", "done")
    return {"master_mkv": str(mkv), "master_wav": str(wav)}


if __name__ == "__main__":
    import sys

    run(sys.argv[1])
