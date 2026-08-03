"""Stage 4 — Render clips.

Boucle sur les pistes du manifest et coupe :
- Audio : depuis master.wav -> MP3 VBR (`libmp3lame -q:a 0`) dans build/audio/,
  une piste à la fois.
- Vidéo : depuis master.mkv -> **un seul MP4** couvrant tout le concert
  (du début de la 1re piste à la fin de la dernière — pas de découpe par
  piste), dans build/video/. Décision du 2026-08-02 : la lecture "morceau par
  morceau" (playlist/série) sur Jellyfin est pénible pour l'utilisateur ; un
  fichier complet évite aussi de re-render la vidéo à chaque ajustement de
  timecode d'une piste (seul l'audio en dépend encore).

Chaque piste dont start/end est renseigné est rendue (audio) ; les pistes
sans timecode sont ignorées, y compris pour le calcul des bornes vidéo.
Stage idempotent (skip si le fichier existe déjà et --force absent).
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable

from ..manifest import Manifest, download_stem

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


def _progress_reader(stream, on_seconds: Callable[[float], None]):
    """Lit le flux `-progress` de ffmpeg et remonte la position encodée.

    ffmpeg écrit des lignes `clé=valeur` (`out_time_ms=…`, `progress=…`) toutes
    les ~0,5 s. La lecture se fait dans un thread : la boucle principale doit
    rester libre de sonder l'annulation et la pause au rythme qui est le sien.
    `out_time_ms` est en **microsecondes** malgré son nom — erreur classique,
    elle donnait une progression 1000× trop rapide.
    """
    def run() -> None:
        try:
            for line in stream:
                key, _, value = line.strip().partition("=")
                if key != "out_time_ms" or not value.isdigit():
                    continue
                on_seconds(int(value) / 1_000_000)
        except Exception:
            # La progression est un confort d'affichage : sa panne ne doit
            # jamais interrompre un encodage qui, lui, se déroule bien.
            pass
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


def _run(cmd: list[str], cancel: Callable[[], bool] | None = None,
         paused: Callable[[], bool] | None = None,
         on_seconds: Callable[[float], None] | None = None) -> None:
    if cancel is None and paused is None and on_seconds is None:
        subprocess.run(cmd, check=True, capture_output=True)
        return
    # Un encodage vidéo dure plusieurs minutes : on ne peut pas attendre la fin
    # du process pour honorer une annulation ou une pause, il faut agir dessus
    # en cours de route.
    proc = subprocess.Popen(
        cmd, stderr=subprocess.DEVNULL, text=True,
        stdout=subprocess.PIPE if on_seconds else subprocess.DEVNULL)
    if on_seconds:
        _progress_reader(proc.stdout, on_seconds)
    frozen = False
    try:
        while True:
            try:
                proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel and cancel():
                # Un process gelé n'observe rien : le réveiller avant de le
                # tuer, sinon le SIGTERM resterait en attente indéfiniment.
                if frozen:
                    proc.send_signal(signal.SIGCONT)
                    frozen = False
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise Cancelled()
            # SIGSTOP fige le process sans rien perdre : il libère le CPU
            # immédiatement et reprend exactement où il en était au SIGCONT.
            want = bool(paused and paused())
            if want and not frozen:
                proc.send_signal(signal.SIGSTOP)
                frozen = True
            elif not want and frozen:
                proc.send_signal(signal.SIGCONT)
                frozen = False
    finally:
        if frozen and proc.poll() is None:
            proc.send_signal(signal.SIGCONT)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def render_audio(master_wav: Path, start: float, end: float, out: Path,
                 cancel: Callable[[], bool] | None = None,
                 paused: Callable[[], bool] | None = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start
    _run([
        "ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
        "-i", str(master_wav),
        "-c:a", "libmp3lame", "-q:a", "0",
        # Format imposé : le rendu écrit d'abord dans un `<nom>.mp3.part`, dont
        # l'extension ne dit rien à ffmpeg (cf. _render_or_cleanup).
        "-f", "mp3", str(out),
    ], cancel, paused)


def render_video(master_mkv: Path, start: float, end: float, out: Path,
                 cancel: Callable[[], bool] | None = None,
                 paused: Callable[[], bool] | None = None,
                 on_progress: Callable[[float], None] | None = None) -> None:
    """`on_progress(frac)` reçoit l'avancement réel 0→1 du ré-encodage.

    Sans lui, l'interface restait figée à 0 % pendant les 15 à 20 minutes du
    ré-encodage d'un concert : l'ancien code n'émettait qu'un « 0/1 » au début
    et un « 1/1 » à la fin.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start
    def _frac(sec: float) -> None:
        # Bornée : ffmpeg peut dépasser légèrement la durée demandée en fin de
        # flux, et une barre qui passe 100 % se lit comme un bug.
        on_progress(max(0.0, min(1.0, sec / duration)))

    on_seconds = _frac if (on_progress and duration > 0) else None
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
        "ffmpeg", "-y", "-nostats", "-progress", "pipe:1",
        "-ss", f"{start:.3f}", "-i", str(master_mkv),
        "-t", f"{duration:.3f}",
        "-c:v", VIDEO_CODEC, "-crf", VIDEO_CRF, "-preset", VIDEO_PRESET,
        "-c:a", "aac", "-b:a", "256k",
        "-movflags", "+faststart",
        # Idem audio : la sortie est un `<nom>.mp4.part` pendant l'encodage.
        "-f", "mp4", str(out),
    ], cancel, paused, on_seconds)


def _expected_filenames(m: Manifest, ext: str) -> set[str]:
    names = set()
    for track in m.tracks:
        if track.get("start") is None or track.get("end") is None:
            continue
        names.add(m.track_filename(track, ext))
    return names


def _purge_orphans(dir_: Path, expected: set[str]) -> None:
    """Retire les fichiers d'un rendu précédent qui ne correspondent plus à
    ce qui est attendu (piste renommée/supprimée, ou changement de nom du
    fichier vidéo complet suite à une modif d'artiste/titre/date). Sans ça,
    un re-rendu sur un album déjà publié laisse des fichiers fantômes dans le
    ZIP et dans la bibliothèque Jellyfin."""
    if not dir_.exists():
        return
    for f in dir_.iterdir():
        if f.is_file() and f.name not in expected:
            f.unlink()


def _render_or_cleanup(fn, src: Path, start, end, out: Path,
                       cancel: Callable[[], bool] | None,
                       paused: Callable[[], bool] | None = None,
                       **kw) -> None:
    """Rend dans un fichier temporaire, puis le met en place d'un seul geste.

    Le stage est idempotent par nom de fichier : un fichier partiel portant le
    nom final serait pris pour un rendu abouti et **jamais** réencodé. C'est ce
    qui s'est produit le 2026-08-03 : un redéploiement a recréé le conteneur du
    worker en plein encodage, laissant un MP4 tronqué de 744 Mo sous son nom
    définitif — une relance l'aurait servi comme vidéo finale.

    Une annulation propre nettoyait bien son fichier, mais un `SIGKILL` (docker,
    OOM, redémarrage de l'hôte) ne laisse aucune chance de le faire. D'où le
    `.part` : le nom final n'apparaît qu'une fois l'encodage terminé, et tout
    résidu `.part` est balayé par la purge des orphelins au rendu suivant.
    """
    # Le nom temporaire ne se termine **pas** par l'extension du média : sinon
    # les `glob("*.mp3")` du catalogue le compteraient comme une piste rendue
    # (un résidu suffirait à sortir l'album des brouillons). ffmpeg déduisant le
    # conteneur de l'extension, `render_audio`/`render_video` lui imposent leur
    # format explicitement — c'est la contrepartie de ce choix.
    part = out.with_name(out.name + ".part")
    part.unlink(missing_ok=True)
    try:
        fn(src, float(start), float(end), part, cancel, paused, **kw)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, out)


def video_filename(m: Manifest, project_slug: str) -> str:
    """Nom du MP4 complet : suit le même schéma que les ZIP de téléchargement
    (`download_stem`) pour rester cohérent et se renommer automatiquement en
    cas de correction d'artiste/titre/date."""
    return f"{download_stem(m.data, project_slug)}_concert-complet.mp4"


def run(project_dir: str | Path, force: bool = False, video: bool = True,
        audio: bool = True,
        on_track: Callable[[int, int, str], None] | None = None,
        on_video: Callable[[float], None] | None = None,
        cancel: Callable[[], bool] | None = None,
        paused: Callable[[], bool] | None = None) -> dict:
    """`on_track(done, total, title)` est appelé avant chaque piste audio.
    `on_video(frac)` reçoit l'avancement réel 0→1 du ré-encodage vidéo.

    `audio=False` rend **uniquement** la vidéo : c'est le job de phase 2, lancé
    après que l'album audio est déjà en place. Il ne touche alors ni à
    `build/audio` ni à sa purge d'orphelins.

    `cancel()` est sondé pendant les encodages : s'il passe à True, le ffmpeg
    en cours est tué, le fichier partiel supprimé, et Cancelled est levée.
    `paused()` gèle/dégèle le ffmpeg en cours (SIGSTOP/SIGCONT) sans rien
    perdre du travail déjà effectué."""
    project_dir = Path(project_dir)
    m = Manifest.load(project_dir / "manifest.yaml")
    master_wav = project_dir / m.data["source"]["master_wav"]
    master_mkv = project_dir / m.data["source"]["master_mkv"]
    audio_dir = project_dir / "build" / "audio"
    video_dir = project_dir / "build" / "video"

    todo = [t for t in m.tracks
            if t.get("start") is not None and t.get("end") is not None]
    total = len(todo)

    rendered = {"audio": [], "video": []}
    if audio:
        _purge_orphans(audio_dir, _expected_filenames(m, "mp3"))
        for i, track in enumerate(todo):
            start, end = track["start"], track["end"]
            if on_track:
                on_track(i, total, str(track.get("title", "")))
            a_out = audio_dir / m.track_filename(track, "mp3")
            if force or not a_out.exists():
                _render_or_cleanup(render_audio, master_wav, start, end,
                                   a_out, cancel, paused)
            rendered["audio"].append(str(a_out))

        if on_track and total:
            on_track(total, total, "")

    # Vidéo : un seul fichier, du début de la 1re piste à la fin de la
    # dernière (master.mkv peut être absent en test audio-only).
    #
    # ⚠️ Aucune purge de `build/video` quand `video` est faux : le job audio de
    # phase 1 tourne précisément avec `video=False` et détruirait sinon le MP4
    # déjà produit — c'est la régression que le découplage rendait possible.
    if video and master_mkv.exists() and todo:
        v_start, v_end = todo[0]["start"], todo[-1]["end"]
        v_name = video_filename(m, project_dir.name)
        _purge_orphans(video_dir, {v_name})
        v_out = video_dir / v_name
        if force or not v_out.exists():
            _render_or_cleanup(render_video, master_mkv, v_start, v_end,
                               v_out, cancel, paused, on_progress=on_video)
        elif on_video:
            on_video(1.0)
        rendered["video"].append(str(v_out))
    elif video:
        _purge_orphans(video_dir, set())

    if audio:
        m.set_state("render", "done")
    else:
        m.set_state("video", "done")
    return rendered


if __name__ == "__main__":
    import sys

    run(sys.argv[1], force="--force" in sys.argv)
