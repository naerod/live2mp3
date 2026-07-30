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

import os
import subprocess
from pathlib import Path
from typing import Callable

from ..manifest import Manifest

# Réglages d'encodage vidéo, surchargeables sans redéploiement.
#
# Défauts mesurés sur un concert d'1 h (source YouTube AV1 à 1,9 Mbps), CT110
# 8 cœurs — voir la campagne de mesures du 2026-07-30 :
#   x264 crf 18 veryfast : 3,9 Go, 21 min, SSIM 0,9943  (ancien défaut)
#   x264 crf 23 veryfast : 2,4 Go, 20 min, SSIM 0,9904  (défaut actuel)
#   x265 crf 26 fast     : 1,4 Go, 75 min, SSIM 0,9859
# CRF 23 divise le poids par 1,6 sans coût en temps ni perte visible. Le HEVC
# gagne 1 Go de plus mais quadruple la durée : à n'activer que si le stockage
# redevient plus contraint que le temps de rendu.
VIDEO_CODEC = os.environ.get("L2M_VIDEO_CODEC", "libx264")
VIDEO_CRF = os.environ.get("L2M_VIDEO_CRF", "23")
VIDEO_PRESET = os.environ.get("L2M_VIDEO_PRESET", "veryfast")


class Cancelled(Exception):
    """L'utilisateur a demandé l'arrêt du rendu."""


def _run(cmd: list[str], cancel: Callable[[], bool] | None = None) -> None:
    if cancel is None:
        subprocess.run(cmd, check=True, capture_output=True)
        return
    # Un encodage vidéo dure plusieurs minutes : on ne peut pas attendre la fin
    # du process pour honorer une annulation, il faut le tuer en cours de route.
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    while True:
        try:
            proc.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if not cancel():
                continue
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise Cancelled()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def render_audio(master_wav: Path, start: float, end: float, out: Path,
                 cancel: Callable[[], bool] | None = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start
    _run([
        "ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
        "-i", str(master_wav),
        "-c:a", "libmp3lame", "-q:a", "0",
        str(out),
    ], cancel)


def render_video(master_mkv: Path, start: float, end: float, out: Path,
                 cancel: Callable[[], bool] | None = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start
    # Seek d'entrée avant -i (rapide) + re-encode pour coupe frame-accurate.
    # Le ré-encodage n'est pas gratuit mais il est indispensable : une copie de
    # flux ne pourrait couper que sur une image-clé (3 à 7 s d'intervalle sur
    # une source YouTube), ce qui ruinerait des timecodes réglés à la
    # milliseconde. Il garantit aussi une sortie H.264 lisible partout, là où
    # la source AV1 forcerait Jellyfin à transcoder à chaque lecture.
    # `+faststart` place l'index en tête de fichier : sans lui, toute lecture
    # progressive (aperçu Drive, lecture web) doit d'abord aller chercher la
    # fin du fichier.
    _run([
        "ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(master_mkv),
        "-t", f"{duration:.3f}",
        "-c:v", VIDEO_CODEC, "-crf", VIDEO_CRF, "-preset", VIDEO_PRESET,
        "-c:a", "aac", "-b:a", "256k",
        "-movflags", "+faststart",
        str(out),
    ], cancel)


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


def _render_or_cleanup(fn, src: Path, start, end, out: Path,
                       cancel: Callable[[], bool] | None) -> None:
    """Un ffmpeg tué laisse un fichier tronqué : sans ce nettoyage, le stage
    étant idempotent par nom de fichier, un rendu relancé après annulation
    conserverait la piste incomplète."""
    try:
        fn(src, float(start), float(end), out, cancel)
    except Cancelled:
        out.unlink(missing_ok=True)
        raise


def run(project_dir: str | Path, force: bool = False, video: bool = True,
        on_track: Callable[[int, int, str], None] | None = None,
        cancel: Callable[[], bool] | None = None) -> dict:
    """`on_track(done, total, title)` est appelé avant chaque piste : le
    ré-encodage vidéo dure plusieurs minutes par piste, sans ça l'UI reste
    figée sur « en cours… » pendant tout le stage.

    `cancel()` est sondé pendant les encodages : s'il passe à True, le ffmpeg
    en cours est tué, le fichier partiel supprimé, et Cancelled est levée."""
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
            _render_or_cleanup(render_audio, master_wav, start, end,
                               a_out, cancel)
        rendered["audio"].append(str(a_out))
        # Vidéo (optionnelle : master.mkv peut être absent en test audio-only)
        if video and master_mkv.exists():
            v_out = video_dir / m.track_filename(track, "mp4")
            if force or not v_out.exists():
                _render_or_cleanup(render_video, master_mkv, start, end,
                                   v_out, cancel)
            rendered["video"].append(str(v_out))

    if on_track and total:
        on_track(total, total, "")
    m.set_state("render", "done")
    return rendered


if __name__ == "__main__":
    import sys

    run(sys.argv[1], force="--force" in sys.argv)
