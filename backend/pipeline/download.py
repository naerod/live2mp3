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
    cmd = ["yt-dlp", "--no-playlist", "--newline",
           # Résilience face aux HTTP 403 intermittents de YouTube (throttling
           # de l'IP quand on enchaîne beaucoup de téléchargements, cf. incident
           # 2026-08-13) : retries + backoff exponentiel + pacing des requêtes.
           "--retries", "10", "--fragment-retries", "10",
           "--extractor-retries", "3", "--retry-sleep", "http:exp=1:30",
           "--sleep-requests", "1.5", "--socket-timeout", "30"]
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
                   progress: ProgressCb | None = None,
                   stem: str = "master_audio") -> Path:
    """Meilleur flux audio seul ; renvoie le fichier téléchargé.

    `stem` : base du nom de sortie — distincte par clip en mode multi-liens
    pour éviter que les téléchargements successifs ne s'écrasent.
    """
    source_dir.mkdir(parents=True, exist_ok=True)
    pattern = source_dir / f"{stem}.%(ext)s"
    cmd = _base_cmd(cookies) + ["-f", "ba/b", "-o", str(pattern), url]
    _run_ytdlp(cmd, progress)
    candidates = sorted(source_dir.glob(f"{stem}.*"))
    if not candidates:
        raise RuntimeError("téléchargement audio : aucun fichier produit")
    return candidates[0]


def _download_audio_retry(url: str, source_dir: Path, cookies: str | None,
                          progress: ProgressCb | None, stem: str,
                          attempts: int = 4) -> Path:
    """download_audio avec réessais : un 403 intermittent (throttling YouTube)
    se dissipe presque toujours à une nouvelle extraction. On repart propre à
    chaque tentative (purge du stem) et on attend, en backoff, avant de réessayer.
    """
    import time
    last: Exception | None = None
    for k in range(attempts):
        try:
            return download_audio(url, source_dir, cookies, progress, stem=stem)
        except RuntimeError as exc:
            last = exc
            for partial in source_dir.glob(f"{stem}.*"):
                partial.unlink(missing_ok=True)
            if k < attempts - 1:
                time.sleep(5 * (k + 1))
    raise RuntimeError(
        f"téléchargement échoué après {attempts} tentatives : {last}")


def extract_wav(master: Path, out_wav: Path) -> Path:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    # Garde-fou : ffmpeg échoue toujours si l'entrée == la sortie (cas atteint
    # si un clip_XX.wav résiduel est re-sélectionné comme source, cf. incident
    # 2026-08-13). On refuse explicitement plutôt que de laisser une erreur
    # ffmpeg opaque remonter à l'utilisateur.
    if master.resolve() == out_wav.resolve():
        raise RuntimeError(
            f"extract_wav : source et destination identiques ({out_wav.name}) — "
            "fichier résiduel d'un essai précédent")
    subprocess.run([
        "ffmpeg", "-y", "-i", str(master),
        "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
        str(out_wav),
    ], check=True, capture_output=True)
    return out_wav


def _wav_seconds(wav: Path) -> float:
    """Durée exacte d'un WAV, mesurée par ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(wav)],
        capture_output=True, text=True, check=True)
    return float((out.stdout or "0").strip() or 0.0)


def run_multi(project_dir: Path, clips: list[dict],
              progress: ProgressCb | None = None) -> dict:
    """Mode multi-liens : télécharge chaque clip, concatène en un master.wav
    unique, puis pose les timecodes exacts (frontières = durées réelles).

    Chaque clip devient une piste ; les frontières tombent pile aux jointures,
    donc aucune détection IA n'est nécessaire (pistes `locked`). Le reste du
    pipeline (waveform, render, tags, artwork, disque) est identique au mode
    mono-lien : il ne voit qu'un master.wav et une setlist déjà découpée.
    """
    m = Manifest.load(project_dir / "manifest.yaml")
    cookies = os.environ.get("YTDLP_COOKIES")
    source_dir = project_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    # Repartir propre : purge des segments d'un essai précédent. Sans ça, un
    # clip_XX.wav résiduel serait re-sélectionné comme source par download_audio
    # (glob "clip_XX.*" + tri alphabétique) → ffmpeg entrée == sortie → échec
    # au réessai (incident 2026-08-13).
    for stale in source_dir.glob("clip_*"):
        stale.unlink(missing_ok=True)
    n = len(clips)
    wavs: list[Path] = []
    durations: list[float] = []
    for i, clip in enumerate(clips):
        # Progression globale : le téléchargement des clips occupe 90 %,
        # répartis équitablement ; les 10 % restants couvrent la concaténation.
        def clip_pct(pct: float, base=i) -> None:
            if progress:
                progress((base + pct / 100.0) / n * 90.0)
        master = _download_audio_retry(clip["url"], source_dir, cookies,
                                       clip_pct, stem=f"clip_{i:02d}")
        wav = extract_wav(master, source_dir / f"clip_{i:02d}.wav")
        durations.append(_wav_seconds(wav))   # mesure avant toute suppression
        wavs.append(wav)
        master.unlink(missing_ok=True)         # le WAV décodé suffit désormais

    if progress:
        progress(92.0)
    out_wav = project_dir / m.data["source"]["master_wav"]
    # Concaténation lossless via le filtre concat (formats déjà alignés :
    # 44,1 kHz stéréo PCM après extract_wav).
    inputs: list[str] = []
    for w in wavs:
        inputs += ["-i", str(w)]
    filt = "".join(f"[{k}:a]" for k in range(n)) + f"concat=n={n}:v=0:a=1[out]"
    subprocess.run(
        ["ffmpeg", "-y", *inputs, "-filter_complex", filt, "-map", "[out]",
         "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2", str(out_wav)],
        check=True, capture_output=True)
    for w in wavs:
        w.unlink(missing_ok=True)   # les segments ne servent plus

    # Timecodes exacts = bornes cumulées des durées réelles des segments.
    # Une piste par clip, dans l'ordre ; `locked` pour que l'IA n'y touche pas.
    t = 0.0
    bounds: list[tuple[float, float]] = []
    for d in durations:
        bounds.append((t, t + d))
        t += d
    for track, (start, end) in zip(m.tracks, bounds):
        track["start"] = round(start, 3)
        track["end"] = round(end, 3)
        track["locked"] = True
    m.data["source"]["duration"] = round(t, 3)
    m.set_state("download", "done")   # persiste aussi les timecodes ci-dessus
    if progress:
        progress(100.0)
    return {"master": str(out_wav), "master_wav": str(out_wav),
            "durations": durations}


def reorder_master(project_dir: str | Path, order: list[int]) -> dict:
    """Réordonne un album multi-liens en re-concaténant le master.

    `order` : permutation des index 0-based des pistes actuelles, dans le nouvel
    ordre voulu. Chaque piste étant un segment contigu de master.wav, on découpe
    le master par timecodes, on ré-assemble dans le nouvel ordre, puis on repose
    des timecodes cumulés (monotones) — l'éditeur et le CUE d'un CD audio
    restent donc valides. Titres/artistes suivent leur piste ; `source.clips`
    est réordonné en parallèle (cohérence d'un éventuel re-téléchargement).

    Réservé aux albums multi-liens (`source.clips`) : sur une source unique, les
    pistes ne sont pas des chansons entières indépendantes (gaps, transitions).
    """
    project_dir = Path(project_dir)
    m = Manifest.load(project_dir / "manifest.yaml")
    src = m.data.get("source", {})
    clips = src.get("clips")
    if not clips:
        raise ValueError("réordonnancement réservé aux albums multi-liens")
    tracks = m.tracks
    n = len(tracks)
    if sorted(order) != list(range(n)):
        raise ValueError("ordre invalide (doit être une permutation des pistes)")
    master = project_dir / src["master_wav"]
    if not master.is_file():
        raise ValueError("master.wav absent : rouvrir l'éditeur pour le régénérer")

    # Index canonique = ordre temporel (par `start`), identique à celui de
    # l'éditeur (EDIT trié par start) : c'est sur cet ordre que porte la
    # permutation reçue. La liste du manifeste peut différer de l'ordre
    # temporel ; on apparie chaque piste à son clip (positions parallèles
    # écrites par run_multi) avant de trier.
    if len(clips) != n:
        raise ValueError("incohérence pistes/clips dans le manifeste")
    paired = sorted(zip(tracks, clips), key=lambda p: float(p[0]["start"]))

    source_dir = project_dir / "source"
    segs: list[Path] = []
    try:
        # Découpe chaque piste dans l'ordre VOULU (segments PCM, précis).
        for pos, idx in enumerate(order):
            t = paired[idx][0]
            seg = source_dir / f"reorder_{pos:02d}.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(master), "-ss", str(float(t["start"])),
                 "-to", str(float(t["end"])), "-c:a", "pcm_s16le",
                 "-ar", "44100", "-ac", "2", str(seg)],
                check=True, capture_output=True)
            segs.append(seg)
        # Ré-assemblage -> master temporaire puis remplacement atomique.
        tmp_master = source_dir / "master.reorder.wav"
        inputs: list[str] = []
        for s in segs:
            inputs += ["-i", str(s)]
        filt = "".join(f"[{k}:a]" for k in range(n)) + f"concat=n={n}:v=0:a=1[out]"
        subprocess.run(
            ["ffmpeg", "-y", *inputs, "-filter_complex", filt, "-map", "[out]",
             "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2", str(tmp_master)],
            check=True, capture_output=True)
        tmp_master.replace(master)
    finally:
        for s in segs:
            s.unlink(missing_ok=True)

    # Timecodes cumulés depuis les durées réelles des segments réordonnés.
    new_tracks, new_clips, t0 = [], [], 0.0
    for pos, idx in enumerate(order):
        old, clip = paired[idx]
        dur = float(old["end"]) - float(old["start"])
        nt = dict(old)
        nt["n"] = pos + 1
        nt["start"] = round(t0, 3)
        nt["end"] = round(t0 + dur, 3)
        nt["locked"] = True
        new_tracks.append(nt)
        new_clips.append(clip)
        t0 += dur
    m.data["tracks"] = new_tracks
    m.data["source"]["clips"] = new_clips
    m.data["source"]["duration"] = round(t0, 3)
    m.save()
    return {"tracks": len(new_tracks), "duration": round(t0, 3)}


def run(project_dir: str | Path, progress: ProgressCb | None = None) -> dict:
    project_dir = Path(project_dir)
    m = Manifest.load(project_dir / "manifest.yaml")
    src = m.data.get("source", {})
    if src.get("clips"):
        return run_multi(project_dir, src["clips"], progress)
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
    if not src.get("url") and not src.get("clips"):
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
