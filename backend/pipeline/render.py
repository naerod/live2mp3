"""Stage 4 — Render clips.

Boucle sur les pistes du manifest et coupe :
- Audio : depuis master.wav -> MP3 VBR (`libmp3lame -q:a 0`) dans build/audio/
- Vidéo : depuis master.mkv -> MP4 (`libx264 -crf 18 -preset veryfast`,
  coupe frame-accurate) dans build/video/

Chaque piste dont start/end est renseigné est rendue ; les pistes sans
timecode sont ignorées. Stage idempotent (skip si le fichier existe déjà
et --force absent).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

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
    # preset veryfast : le CRF (donc la qualité perçue) est inchangé, seule
    # l'efficacité de compression baisse — fichiers ~20-30 % plus lourds pour
    # un encodage ~4x plus rapide. Les masters YouTube tournent autour de
    # 2 Mbps quand la sortie CRF 18 en fait 10 : la qualité est plafonnée par
    # la source, pas par l'encodeur.
    _run([
        "ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(master_mkv),
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "256k",
        str(out),
    ])


def _expected_filenames(m: Manifest, ext: str) -> set[str]:
    names = set()
    for track in m.tracks:
        if track.get("start") is None or track.get("end") is None:
            continue
        names.add(m.track_filename(track, ext))
    return names


def _purge_orphans(dir_: Path, expected: set[str]) -> None:
    """Retire les fichiers d'un rendu précédent qui ne correspondent plus à
    aucune piste courante (piste renommée, fusionnée ou supprimée depuis un
    précédent rendu). Sans ça, un re-rendu sur un album déjà publié laisse des
    pistes fantômes dans le ZIP et dans la bibliothèque Jellyfin."""
    if not dir_.exists():
        return
    for f in dir_.iterdir():
        if f.is_file() and f.name not in expected:
            f.unlink()


def run(project_dir: str | Path, force: bool = False, video: bool = True,
        on_track: Callable[[int, int, str], None] | None = None) -> dict:
    """`on_track(done, total, title)` est appelé avant chaque piste : le
    ré-encodage vidéo dure plusieurs minutes par piste, sans ça l'UI reste
    figée sur « en cours… » pendant tout le stage."""
    project_dir = Path(project_dir)
    m = Manifest.load(project_dir / "manifest.yaml")
    master_wav = project_dir / m.data["source"]["master_wav"]
    master_mkv = project_dir / m.data["source"]["master_mkv"]
    audio_dir = project_dir / "build" / "audio"
    video_dir = project_dir / "build" / "video"

    _purge_orphans(audio_dir, _expected_filenames(m, "mp3"))
    if video:
        _purge_orphans(video_dir, _expected_filenames(m, "mp4"))

    todo = [t for t in m.tracks
            if t.get("start") is not None and t.get("end") is not None]
    total = len(todo)

    rendered = {"audio": [], "video": []}
    for i, track in enumerate(todo):
        start, end = track["start"], track["end"]
        if on_track:
            on_track(i, total, str(track.get("title", "")))
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

    if on_track and total:
        on_track(total, total, "")
    m.set_state("render", "done")
    return rendered


if __name__ == "__main__":
    import sys

    run(sys.argv[1], force="--force" in sys.argv)
