"""Re-couper une piste existante à la waveform — vue 3 pistes avec cadenas.

Sur un album live, les chansons **s'enchaînent** presque toujours :
la fin de la piste N vaut le début de la piste N+1. Ré-ajuster une
frontière ne bouge alors PAS une seule piste : elle rallonge la piste
courante et raccourcit sa voisine (ou l'inverse), sinon on introduit
un gap ou un chevauchement inaudible en écoute continue.

L'écran de recut ouvre donc sur **jusqu'à 3 pistes** (précédente,
courante, suivante) autour de N. Les frontières partagées (`end[N-1]`
== `start[N]`, `end[N]` == `start[N+1]`) portent un **cadenas** dont
le comportement est calqué sur l'éditeur Peaks.js d'album (cf. NOTES
2026-07-19) : cadenas fermé = les deux pistes suivent la même valeur
(un seul curseur bouge les deux), cadenas ouvert = curseurs
indépendants (introduit un gap/chevauchement volontaire).

Cas gérés selon la nature de la piste :

- **Concert mono-source** (`source.master_wav` seul) → chemin nominal.
  On extrait de master.wav une tranche couvrant `[N-1.start - PAD,
  N+1.end + PAD]` (PAD = 3 s, réduit vs la v1 qui montrait une piste
  seule ; ici le contexte visuel vient déjà des voisines). Waveform +
  preview servis. À la validation, les timecodes des pistes modifiées
  sont écrits dans le manifest, et **chaque piste modifiée** est
  ré-encodée depuis le master. Les pistes non modifiées ne sont pas
  touchées (mtime préservés).

- **Piste externe** (`track.source.url` : piste ajoutée par addtrack,
  sans timecodes d'album) → fallback re-download via
  `download.download_audio` sur la source d'origine, puis
  `encode_track` remplace le MP3 et met à jour
  `track.source.start`/`end`/`recut_at`. Pas de cadenas ni de voisines :
  une piste externe n'a pas de frontière partagée avec ses voisines.

- **Multi-liens** (`source.clips` présents, cf. compilations
  `live-crossovers`) → **refusé (409)**. Chaque piste vient d'un clip
  indépendant, sans frontière logique avec ses voisines : le geste
  perd son sens et introduirait des recompositions de master lourdes
  pour un gain nul. On renvoie vers l'éditeur complet
  (`btn-open-editor` → `/app#{slug}`).

Décision d'architecture : le master (`source/master.wav`, `.mkv`) est
CONSERVÉ en permanence sur l'infrastructure — `L2M_PURGE_MASTERS` n'est
activé nulle part. Le chemin nominal lit `master.wav` directement, sans
aucun re-téléchargement.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import addtrack, jellyfin
from . import manifest as _manifest_mod
from .addtrack import (
    JOB_TTL_S,
    _JOBS,
    _JOBS_LOCK,
    _job_get,
    _job_set,
    _purge_jobs,
    _wav_seconds,
    encode_track,
)
from .auth import require_gestionnaire
from .manifest import PROJECTS_DIR, Manifest
from .pipeline import download, preanalyze, render

router = APIRouter(tags=["recut"])

# Contexte visuel de part et d'autre du triptyque N-1..N+1. 3 s suffisent :
# les voisines fournissent déjà le contexte utile, on n'a besoin que d'un peu
# de marge pour visualiser une frontière au bord de la tranche.
CONTEXT_PAD_S = 3.0

# Deux frontières partagées peuvent être considérées « liées » côté UI si
# leurs timecodes coïncident à moins de cette tolérance. Le manifest ne
# persiste pas l'état lié/délié (c'est un fait déductible), mais l'éditeur
# de coupes utilise la même astuce (cf. NOTES 2026-07-19).
LINK_TOL_S = 0.05


# --- Cas de figure ---------------------------------------------------------
def _track_kind(m: Manifest, track: dict) -> str:
    """« external » / « mono » / « multi » — quel chemin de recut appliquer."""
    if render.is_external(track):
        return "external"
    src = m.data.get("source", {}) or {}
    if src.get("clips"):
        return "multi"
    return "mono"


def _find_track_index(m: Manifest, n: int) -> int:
    """Position 0-indexée dans `m.tracks` de la piste de numéro `n`."""
    for i, t in enumerate(m.tracks):
        if int(t.get("n", -1)) == n:
            return i
    raise HTTPException(404, "piste introuvable")


def _master_wav_path(project_dir: Path, m: Manifest) -> Path:
    """Master WAV attendu par le pipeline ; 409 s'il a été purgé.

    Une piste concert ne peut être re-coupée sans le master : c'est la seule
    source PCM lossless disponible. En rouvrant l'éditeur audio le pipeline
    le re-télécharge tout seul — mieux vaut y renvoyer.
    """
    rel = (m.data.get("source", {}) or {}).get("master_wav") or "source/master.wav"
    wav = project_dir / rel
    if not wav.is_file():
        raise HTTPException(
            409, "master.wav absent — rouvrir l'éditeur audio pour le régénérer")
    return wav


# --- Préparation de la waveform de la piste à re-couper --------------------
def _recut_dir(token: str) -> Path:
    """Espace de travail dédié : voisin de `.l2m-addtrack/prep-*`, même TTL,
    sous le volume projects/. Sa purge est gérée par `_sweep_recut_dirs`."""
    return PROJECTS_DIR / ".l2m-addtrack" / f"recut-{token}"


def _sweep_recut_dirs() -> None:
    """Efface les préparations recut abandonnées (onglet fermé) au-delà du TTL.

    Symétrique de `addtrack._sweep_prep_dirs` : `prep-*` et `recut-*` cohabitent
    sous la même racine mais chacun balaye ses propres traces.
    """
    root = PROJECTS_DIR / ".l2m-addtrack"
    if not root.exists():
        return
    cutoff = time.time() - JOB_TTL_S
    for d in root.glob("recut-*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _extract_slice(master: Path, start: float, end: float, out: Path) -> None:
    """Coupe PCM lossless [start, end] du master → out (WAV). Bornes déjà
    validées par l'appelant (0 <= start < end <= master_duration)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
         "-i", str(master), "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2",
         str(out)],
        check=True, capture_output=True)


def _make_preview_mp3(wav: Path, out: Path) -> None:
    """MP3 128 k léger pour la lecture in-browser."""
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav), "-vn", "-c:a", "libmp3lame",
         "-b:a", "128k", str(out)],
        check=True, capture_output=True)


def _neighbours(tracks: list[dict], i: int) -> tuple[dict | None, dict, dict | None]:
    """Retourne (prev, current, next) — prev/next peuvent être None aux bords.

    Les pistes externes (`source.url`) au sein d'un album mixte NE PEUVENT
    PAS servir de voisines de contexte : elles n'ont pas de timecodes
    d'album (cf. addtrack), on ne saurait pas où les positionner dans la
    tranche. On les saute silencieusement — le front verra juste
    « pas de piste précédente » à ce bord.
    """
    def has_bounds(t: dict) -> bool:
        return (t is not None
                and t.get("start") is not None
                and t.get("end") is not None)
    current = tracks[i]
    prev = None
    for j in range(i - 1, -1, -1):
        if has_bounds(tracks[j]):
            prev = tracks[j]
            break
    nxt = None
    for j in range(i + 1, len(tracks)):
        if has_bounds(tracks[j]):
            nxt = tracks[j]
            break
    return prev, current, nxt


def _run_recut_prep_master(token: str, project_dir: Path, m: Manifest,
                           i: int) -> None:
    """Prépare la waveform pour une piste concert et ses voisines.

    On extrait une tranche [prev.start - PAD, next.end + PAD] du master
    (ou N.start / N.end aux bords) et on en fait waveform + preview. Le
    front reçoit la durée + les bornes DE CHAQUE piste visible (dans la
    tranche) + l'état de liaison déduit des égalités start[k]==end[k-1].
    """
    work = _recut_dir(token)
    try:
        _job_set(token, state="running", stage="extract", pct=0.0)
        master = _master_wav_path(project_dir, m)
        master_dur = _wav_seconds(master)
        prev, cur, nxt = _neighbours(m.tracks, i)

        # Bornes de la tranche extraite : englobe prev + cur + nxt quand
        # possible, avec un petit padding pour le confort visuel aux bords.
        span_start = float((prev or cur).get("start") or 0.0)
        span_end = float((nxt or cur).get("end") or master_dur)
        ext_start = max(0.0, span_start - CONTEXT_PAD_S)
        ext_end = min(master_dur, span_end + CONTEXT_PAD_S)
        slice_wav = work / "slice.wav"
        _extract_slice(master, ext_start, ext_end, slice_wav)

        _job_set(token, stage="waveform", pct=0.0)
        preanalyze.generate_waveform(slice_wav, work / "waveform.dat")

        _job_set(token, stage="preview", pct=0.0)
        _make_preview_mp3(slice_wav, work / "preview.mp3")

        duration = _wav_seconds(slice_wav)

        # Position de chaque piste visible DANS la tranche (0..duration).
        def in_slice(t: dict) -> dict:
            s = max(0.0, float(t["start"]) - ext_start)
            e = min(duration, float(t["end"]) - ext_start)
            return {"n": int(t["n"]),
                    "title": t.get("title", "") or "",
                    "start": round(s, 3),
                    "end": round(e, 3)}

        tracks_out: list[dict] = []
        if prev is not None:
            tracks_out.append(in_slice(prev))
        tracks_out.append(in_slice(cur))
        if nxt is not None:
            tracks_out.append(in_slice(nxt))

        # État de liaison des frontières partagées : déduit par égalité au
        # LINK_TOL_S près (au moment de la préparation ; l'utilisateur pourra
        # les délier côté UI). Pas persisté au manifest.
        boundaries: list[dict] = []
        for k in range(len(tracks_out) - 1):
            left, right = tracks_out[k], tracks_out[k + 1]
            linked = abs(left["end"] - right["start"]) <= LINK_TOL_S
            boundaries.append({"left_n": left["n"], "right_n": right["n"],
                               "linked": linked})

        # `slice.wav` a servi (preview + waveform), on l'allège pour que la
        # préparation ne retienne pas un PCM potentiellement gros (30 s de
        # PCM 44,1 kHz stéréo = ~5 Mo, sans risque, mais principe de moindre
        # trace le temps que l'application tarde à consommer le token).
        slice_wav.unlink(missing_ok=True)

        _job_set(token, state="done", stage="done", pct=100.0,
                 duration=duration,
                 ext_start=round(ext_start, 3),
                 ext_end=round(ext_end, 3),
                 master_duration=round(master_dur, 3),
                 tracks=tracks_out,
                 boundaries=boundaries)
    except HTTPException as e:
        # Ne PAS re-raise depuis un thread : l'HTTPException remonterait sans
        # destinataire. L'état est publié dans le job pour que le sondage
        # front reçoive un message lisible.
        shutil.rmtree(work, ignore_errors=True)
        _job_set(token, state="error", error=e.detail or "master absent")
    except Exception as e:
        shutil.rmtree(work, ignore_errors=True)
        _job_set(token, state="error", error=str(e) or e.__class__.__name__)


def _run_recut_prep_external(token: str, url: str,
                             src_meta: dict | None) -> None:
    """Fallback pour une piste externe : re-télécharge la source d'origine.

    Chemin identique à `addtrack._run_prep_bg` (mêmes noms de fichiers,
    même waveform + preview). On expose aussi les bornes actuelles pour
    que le trimmer ouvre déjà callé sur la coupe existante.
    """
    work = _recut_dir(token)
    try:
        _job_set(token, state="running", stage="download", pct=0.0)
        cookies = os.environ.get("YTDLP_COOKIES")
        src = download.download_audio(
            url, work, cookies,
            progress=lambda pct: _job_set(token, stage="download", pct=pct))
        _job_set(token, stage="waveform", pct=0.0)
        wav = download.extract_wav(src, work / "clip.wav")
        preanalyze.generate_waveform(wav, work / "waveform.dat")
        _job_set(token, stage="preview", pct=0.0)
        _make_preview_mp3(wav, work / "preview.mp3")
        duration = _wav_seconds(wav)
        wav.unlink(missing_ok=True)
        # Bornes courantes de la piste externe : reportées telles quelles pour
        # que le trimmer s'ouvre sur la coupe existante.
        cur_start = float((src_meta or {}).get("start") or 0.0)
        cur_end = float((src_meta or {}).get("end") or duration)
        cur_end = min(cur_end, duration)
        _job_set(token, state="done", stage="done", pct=100.0,
                 duration=duration,
                 cur_start=round(cur_start, 3),
                 cur_end=round(cur_end, 3))
    except Exception as e:
        shutil.rmtree(work, ignore_errors=True)
        _job_set(token, state="error", error=str(e) or e.__class__.__name__)


@router.post("/api/albums/{slug}/tracks/{n}/recut/prep")
def recut_prep(slug: str, n: int,
               identity: dict = Depends(require_gestionnaire)) -> dict:
    """Prépare la waveform d'une piste existante pour la re-coupe.

    Mono : lecture directe du master ; renvoie la tranche N-1..N+1 avec
    bornes de chaque piste + état des frontières.
    External : re-télécharge la source (thread + sondage).
    Multi-liens : 409 explicite (non pertinent — cf. docstring module).
    """
    project_dir = PROJECTS_DIR / slug
    mpath = project_dir / "manifest.yaml"
    if not mpath.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(mpath)
    i = _find_track_index(m, n)
    track = m.tracks[i]
    kind = _track_kind(m, track)

    if kind == "multi":
        # Sur un album multi-liens (live-crossovers), chaque piste vient d'une
        # source indépendante — pas de frontière logique avec les voisines et
        # rien à recomposer utilement. Le workflow prod est de rouvrir
        # l'éditeur audio complet (Peaks.js) qui traite chaque clip à sa place.
        raise HTTPException(
            409, "recut par piste indisponible sur un album multi-liens — "
                 "utilisez « Ouvrir l'éditeur audio » pour ce type d'album")

    _purge_jobs()
    addtrack._sweep_prep_dirs()
    _sweep_recut_dirs()
    token = uuid4().hex
    _job_set(token, state="queued", stage="prep", pct=0.0,
             slug=slug, n=n, kind=kind)

    if kind == "external":
        url = ((track.get("source") or {}).get("url") or "").strip()
        if not url:
            raise HTTPException(
                400, "piste externe sans URL source (ré-upload requis)")
        threading.Thread(target=_run_recut_prep_external,
                         args=(token, url, track.get("source")),
                         daemon=True).start()
    else:
        threading.Thread(target=_run_recut_prep_master,
                         args=(token, project_dir, m, i),
                         daemon=True).start()
    return {"ok": True, "token": token, "kind": kind}


def _job_for(slug: str, n: int, token: str) -> dict:
    """Job de préparation, en vérifiant qu'il appartient bien à (slug, piste).

    Le jeton seul suffisait à lire la préparation de n'importe quel album :
    l'URL portait déjà `slug` et `n`, ils n'étaient simplement pas confrontés
    au job. Sans ce contrôle, un gestionnaire pouvait servir la waveform et
    l'aperçu d'un autre album en devinant/réutilisant un jeton.
    """
    job = _job_get(token)
    if not job or job.get("slug") != slug or int(job.get("n", -1)) != n:
        raise HTTPException(404, "préparation inconnue")
    return job


@router.get("/api/albums/{slug}/tracks/{n}/recut/{token}")
def recut_prep_status(slug: str, n: int, token: str,
                      identity: dict = Depends(require_gestionnaire)) -> dict:
    job = _job_for(slug, n, token)
    return {k: v for k, v in job.items() if k != "created_at"}


@router.get("/api/albums/{slug}/tracks/{n}/recut/{token}/waveform.dat")
def recut_prep_waveform(slug: str, n: int, token: str,
                        identity: dict = Depends(require_gestionnaire)):
    _job_for(slug, n, token)
    dat = _recut_dir(token) / "waveform.dat"
    if not dat.exists():
        raise HTTPException(404, "waveform indisponible")
    return FileResponse(str(dat), media_type="application/octet-stream")


@router.get("/api/albums/{slug}/tracks/{n}/recut/{token}/audio")
def recut_prep_audio(slug: str, n: int, token: str,
                     identity: dict = Depends(require_gestionnaire)):
    _job_for(slug, n, token)
    mp3 = _recut_dir(token) / "preview.mp3"
    if not mp3.exists():
        raise HTTPException(404, "aperçu indisponible")
    return FileResponse(str(mp3), media_type="audio/mpeg")


# --- Application de la nouvelle coupe --------------------------------------
class TrackEdit(BaseModel):
    n: int
    start: float
    end: float


class RecutApplyIn(BaseModel):
    token: str
    # Mono-source : liste de pistes modifiées (bornes dans la tranche extraite,
    # 0..duration renvoyée par prep). Au moins la piste ciblée doit y être ;
    # les voisines n'y sont incluses que si l'utilisateur les a déplacées
    # (cadenas fermé ou frontière déliée). Le front envoie ce qui a bougé,
    # rien de plus.
    edits: list[TrackEdit] | None = None
    # External : bornes uniques dans la source complète (comme prep-clip).
    # Conservé pour la compatibilité du chemin external, qui n'a pas de
    # voisines. Ignoré côté mono.
    start: float | None = None
    end: float | None = None


def _tag_one(mp3: Path, m: Manifest, track: dict) -> None:
    """Réapplique les tags ID3 sur UN fichier — équivalent scopé de
    `_write_track_tags` + `_write_album_tags` sans toucher aux voisins.

    `render.render_audio` produit un MP3 nu (`-c:a libmp3lame -q:a 0` sans
    métadonnées) ; sans ça, un lecteur perdrait titre + n° + album/artiste
    au moindre recut.
    """
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3NoHeaderError
    if not mp3.exists():
        return
    total = len(m.tracks)
    pos = int(track.get("n") or 0)
    try:
        tags = EasyID3(str(mp3))
    except ID3NoHeaderError:
        tags = EasyID3()
        tags.save(str(mp3))
        tags = EasyID3(str(mp3))
    alb = m.data.get("album", {}) or {}
    if alb.get("title"):
        tags["album"] = [alb["title"]]
    if alb.get("artist"):
        tags["artist"] = [alb["artist"]]
        tags["albumartist"] = [alb["artist"]]
    date = alb.get("date") or ""
    if date:
        tags["date"] = [date[:4] if len(date) >= 4 else date]
    tags["title"] = [_manifest_mod.numbered_title(pos, track.get("title", ""))]
    tags["tracknumber"] = [f"{pos}/{total}"]
    tags.save()


def _rerender_track_audio(project_dir: Path, m: Manifest, track: dict) -> Path:
    """Re-encode le MP3 d'une piste concert depuis le master courant.

    Le nom de fichier suit `track_filename` : titre inchangé → même nom.
    L'ancien fichier est purgé si le nom a bougé (garde-fou renommage).
    """
    master = _master_wav_path(project_dir, m)
    audio_dir = project_dir / "build" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    expected = m.track_filename(track, "mp3")
    for f in audio_dir.glob(f"{int(track['n']):02d}. *.mp3"):
        if f.name != expected:
            f.unlink(missing_ok=True)
    out = audio_dir / expected
    render._render_or_cleanup(
        render.render_audio, master,
        float(track["start"]), float(track["end"]), out, None)
    return out


def _apply_recut_external(project_dir: Path, m: Manifest, track: dict,
                          token: str, seg_start: float, seg_end: float) -> Path:
    """Ré-encode le MP3 d'une piste externe depuis la source re-préparée."""
    prep = _recut_dir(token)
    files = sorted(prep.glob("master_audio.*"))
    if not files:
        raise HTTPException(410, "source de préparation expirée")
    src = files[0]
    out = (project_dir / "build" / "audio"
           / m.track_filename(track, "mp3"))
    encode_track(src, out, seg_start, seg_end)
    src_meta = track.setdefault("source", {})
    src_meta["start"] = float(seg_start)
    src_meta["end"] = float(seg_end)
    src_meta["recut_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    m.save()
    return out


def _apply_recut_mono(project_dir: Path, m: Manifest, target_n: int,
                      token: str, job: dict,
                      edits: list[TrackEdit]) -> dict:
    """Applique une ou plusieurs éditions de pistes contiguës (mono-source).

    Les bornes reçues sont dans la référence de la tranche extraite ; on les
    remonte au master via `ext_start` (mémorisé dans le job à la préparation).
    On ne re-encode que les pistes réellement mises à jour ; les voisines
    non incluses dans `edits` ne sont pas touchées (mtime préservés).
    """
    if not edits:
        raise HTTPException(400, "aucune édition fournie")
    ext_start = float(job.get("ext_start") or 0.0)
    master_dur = float(job.get("master_duration") or 0.0)
    prep_tracks = {int(t["n"]): t for t in (job.get("tracks") or [])}

    # Validation : chaque piste éditée doit faire partie de la tranche
    # préparée, la piste cible doit être présente, chaque plage doit être
    # bien formée et rester dans les bornes du master.
    edit_by_n: dict[int, TrackEdit] = {}
    for e in edits:
        if int(e.n) not in prep_tracks:
            raise HTTPException(
                400, f"piste {e.n} hors du triptyque préparé")
        if e.end <= e.start:
            raise HTTPException(
                400, f"piste {e.n} : fin ({e.end}) avant début ({e.start})")
        new_start = ext_start + float(e.start)
        new_end = ext_start + float(e.end)
        if new_end > master_dur + 0.01 or new_start < -0.01:
            raise HTTPException(400, f"piste {e.n} : hors master")
        edit_by_n[int(e.n)] = e
    if target_n not in edit_by_n:
        raise HTTPException(
            400, f"la piste cible {target_n} doit figurer dans les édits")

    # Écritures manifest — on retrouve chaque piste par son numéro, on remplace
    # start/end et rien d'autre (title/artist/locked/source sont inchangés).
    rewritten: list[dict] = []
    for tr in m.tracks:
        if int(tr.get("n", -1)) in edit_by_n:
            e = edit_by_n[int(tr["n"])]
            tr["start"] = round(ext_start + float(e.start), 3)
            tr["end"] = round(ext_start + float(e.end), 3)
            rewritten.append(tr)
    m.save()

    # Ré-encode chaque piste modifiée (ordre stable — sans importance ici).
    files: list[dict] = []
    for tr in rewritten:
        out = _rerender_track_audio(project_dir, m, tr)
        try:
            _tag_one(out, m, tr)
        except Exception:
            pass
        files.append({"n": int(tr["n"]), "file": out.name,
                      "start": tr["start"], "end": tr["end"]})
    return {"edited": files}


@router.post("/api/albums/{slug}/tracks/{n}/recut")
def recut_apply(slug: str, n: int, payload: RecutApplyIn,
                identity: dict = Depends(require_gestionnaire)) -> dict:
    """Applique la nouvelle coupe, ré-encode les pistes modifiées, sauvegarde.

    Le rendu est synchrone : re-couper une (ou 2-3) piste(s) tient en
    quelques secondes (rognage PCM + encodage MP3 court), la latence est
    acceptable pour le front. Pas de file de rendu ; les jobs RQ sont
    réservés au rendu global.
    """
    project_dir = PROJECTS_DIR / slug
    mpath = project_dir / "manifest.yaml"
    if not mpath.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(mpath)
    i = _find_track_index(m, n)
    track = m.tracks[i]
    kind = _track_kind(m, track)

    if kind == "multi":
        raise HTTPException(
            409, "recut par piste indisponible sur un album multi-liens")

    job = _job_get(payload.token)
    if (not job or job.get("state") != "done"
            or job.get("slug") != slug or int(job.get("n", -1)) != n):
        raise HTTPException(409, "préparation absente ou expirée")

    try:
        if kind == "external":
            if payload.start is None or payload.end is None:
                raise HTTPException(400, "bornes start/end requises")
            if payload.end <= payload.start:
                raise HTTPException(400, "rognage : fin avant début")
            out = _apply_recut_external(project_dir, m, track,
                                        payload.token, payload.start,
                                        payload.end)
            new_duration = payload.end - payload.start
            info: dict[str, Any] = {"file": out.name,
                                    "duration": round(new_duration, 3)}
        else:
            info = _apply_recut_mono(project_dir, m, n, payload.token, job,
                                     payload.edits or [])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"application impossible : {e}")

    # Dérivés album (waveform, preview.mp3) : régénération best-effort — utile
    # à la prochaine ouverture de l'éditeur complet (le master de mono n'a pas
    # bougé mais les timecodes affichés dépendent du manifest).
    try:
        master = _master_wav_path(project_dir, m)
        preanalyze.generate_waveform(master,
                                     project_dir / "source" / "waveform.dat")
        (project_dir / "source" / "preview.mp3").unlink(missing_ok=True)
        from . import linktool
        linktool.make_preview(project_dir, m)
    except Exception:
        pass

    # Jellyfin ne détecte pas seul un changement de contenu à l'intérieur d'un
    # album déjà monté (cf. addtrack, jellyfin.py) : refresh best-effort.
    try:
        jellyfin.refresh_album(slug)
    except Exception:
        pass

    # Nettoyage de la préparation consommée.
    shutil.rmtree(_recut_dir(payload.token), ignore_errors=True)
    with _JOBS_LOCK:
        _JOBS.pop(payload.token, None)

    return {"ok": True, "n": n, "kind": kind, **info}
