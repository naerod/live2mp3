"""Stage 1 — Download.

Deux modes selon `source.media` du manifest :
- `video` : yt-dlp meilleure qualité -> source/master.mkv (clips MP4 possibles)
- `audio` : meilleur flux audio seul -> source/master_audio.<ext> (léger et
  rapide — c'est le mode par défaut de l'outil, l'album MP3 étant le livrable)

Dans les deux cas : extraction lossless -> source/master.wav (pas de
ré-encodage lossy intermédiaire).

IMPORTANT : n'est appelé qu'avec une URL explicitement fournie par
l'utilisateur (point d'arrêt D). Ne télécharge jamais de sa propre initiative.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Callable

from ..manifest import Manifest

ProgressCb = Callable[[float], None]

_PCT = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")


def _run_ytdlp(cmd: list[str], progress: ProgressCb | None) -> None:
    """Exécute yt-dlp en publiant le pourcentage de téléchargement."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    tail: list[str] = []
    last = -5.0
    assert proc.stdout is not None
    for line in proc.stdout:
        tail.append(line.rstrip())
        if len(tail) > 30:
            tail.pop(0)
        m = _PCT.search(line)
        if m and progress is not None:
            pct = float(m.group(1))
            if pct - last >= 2 or pct >= 100:
                last = pct
                progress(pct)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("yt-dlp a échoué :\n" + "\n".join(tail[-8:]))


def _base_cmd(cookies: str | None) -> list[str]:
    cmd = ["yt-dlp", "--no-playlist", "--newline"]
    if cookies:
        cmd += ["--cookies", cookies]
    return cmd


def download_master(url: str, out_mkv: Path, cookies: str | None = None,
                    progress: ProgressCb | None = None) -> Path:
    out_mkv.parent.mkdir(parents=True, exist_ok=True)
    cmd = _base_cmd(cookies) + ["-f", "bv*+ba/best",
                                "--merge-output-format", "mkv",
                                "-o", str(out_mkv), url]
    _run_ytdlp(cmd, progress)
    return out_mkv


def download_audio(url: str, source_dir: Path, cookies: str | None = None,
                   progress: ProgressCb | None = None) -> Path:
    """Meilleur flux audio seul ; renvoie le fichier téléchargé."""
    source_dir.mkdir(parents=True, exist_ok=True)
    pattern = source_dir / "master_audio.%(ext)s"
    cmd = _base_cmd(cookies) + ["-f", "ba/b", "-o", str(pattern), url]
    _run_ytdlp(cmd, progress)
    candidates = sorted(source_dir.glob("master_audio.*"))
    if not candidates:
        raise RuntimeError("téléchargement audio : aucun fichier produit")
    return candidates[0]


def extract_wav(master: Path, out_wav: Path) -> Path:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-i", str(master),
        "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
        str(out_wav),
    ], check=True, capture_output=True)
    return out_wav


def run(project_dir: str | Path, progress: ProgressCb | None = None) -> dict:
    project_dir = Path(project_dir)
    m = Manifest.load(project_dir / "manifest.yaml")
    src = m.data.get("source", {})
    url = src.get("url")
    if not url:
        raise ValueError("Aucune URL source dans le manifest (point d'arrêt D).")
    cookies = os.environ.get("YTDLP_COOKIES")
    if src.get("media", "video") == "audio":
        master = download_audio(url, project_dir / "source", cookies, progress)
    else:
        master = download_master(url, project_dir / src["master_mkv"], cookies,
                                 progress)
    wav = extract_wav(master, project_dir / src["master_wav"])
    m.set_state("download", "done")
    return {"master": str(master), "master_wav": str(wav)}


def purge_master(project_dir: str | Path) -> dict:
    """Supprime les masters d'un album rendu, en les rendant re-téléchargeables.

    Les masters (MKV + WAV) pèsent ~1,6 Go par album et ne servent qu'à
    re-découper : une fois les pistes produites, ils dorment. On les supprime
    et on repasse `download` à `pending` — rouvrir l'éditeur relance alors la
    préparation, qui re-télécharge depuis `source.url` et régénère la forme
    d'onde. La détection IA, elle, est sautée puisque tous les timecodes
    existent (cf. main.py::prepare) : les coupes réglées à la main survivent.

    Refuse de purger sans URL source : sans elle, la suppression serait
    définitive et l'album ne pourrait plus jamais être re-découpé.
    """
    project_dir = Path(project_dir)
    m = Manifest.load(project_dir / "manifest.yaml")
    src = m.data.get("source", {})
    if not src.get("url"):
        return {"purged": False, "reason": "aucune URL source"}
    freed = 0
    for key in ("master_mkv", "master_wav"):
        rel = src.get(key)
        if not rel:
            continue
        f = project_dir / rel
        if f.is_file():
            freed += f.stat().st_size
            f.unlink()
    if not freed:
        return {"purged": False, "reason": "déjà purgé"}
    m.set_state("download", "pending")
    return {"purged": True, "freed": freed}


if __name__ == "__main__":
    import sys

    run(sys.argv[1])
