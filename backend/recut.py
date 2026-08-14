"""Re-couper une piste existante à la waveform.

Complète `addtrack.py` en s'attaquant au symétrique : au lieu d'*ajouter*
une piste, on rogne à nouveau une piste déjà rendue, poignées début/fin
sur sa forme d'onde exacte. Ces poignées font foi au millième de seconde,
là où l'éditeur Peaks.js de l'album entier n'ouvre pas piste par piste.

Trois cas selon la nature de la piste :

- **Concert mono-source** (`source.master_wav` seul) → la piste K est
  déjà une plage [start,end] de `source/master.wav`. On extrait de ce
  master une tranche autour de la piste (contexte avant/après) et on
  sert waveform + preview au front. À la validation, on ne met à jour
  que `track.start` / `track.end` puis on ré-encode ce **seul** MP3
  depuis le master : les pistes voisines ne sont pas touchées (un gap
  ou un chevauchement se corrige en re-coupant aussi la piste voisine —
  volontairement pas d'effet cascade).

- **Multi-source** (`source.clips` + `source.master_wav`) → chaque piste
  est un segment contigu du master concaténé (cf. `download.run_multi`).
  On propose la même waveform que pour un concert (tranche du master
  avec contexte). À la validation on **re-assemble** le master en
  remplaçant la K-ième tranche par la sous-tranche choisie ; les
  timecodes cumulés de toutes les pistes suivantes sont recalculés
  (monotones), mais seul le MP3 de la piste K est ré-encodé (les
  autres pistes gardent leurs mêmes bytes, seuls leurs offsets dans le
  master ont changé).

- **Piste externe** (`track.source.url` : piste ajoutée par addtrack,
  sans timecodes d'album) → réutilise le flux `prep-clip` de addtrack
  pour re-préparer la source, puis remplace le MP3 (via `encode_track`)
  et met à jour `track.source.start` / `track.source.end`.

Décision d'architecture : le master (`source/master.wav`, `.mkv`) est
CONSERVÉ en permanence sur l'infrastructure — `L2M_PURGE_MASTERS` n'est
activé nulle part (vérifié dans docker-compose.yml et deploy/). Donc le
chemin nominal (≈ 100 % des cas) lit `master.wav` directement, sans
aucun re-téléchargement Internet. Un fallback re-download reste possible
via `track.source.url` (piste externe), mais **pas** pour un concert
dont le master aurait été purgé manuellement — dans ce cas on renvoie
409 en invitant à rouvrir l'éditeur (qui, lui, sait re-préparer). Ce
choix évite de dupliquer le complexe pipeline `prepare` juste pour un
recut ponctuel.
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
from .albumfiles import _rename_audio_files, _write_album_tags, _write_track_tags
from .auth import require_gestionnaire
from .manifest import PROJECTS_DIR, Manifest
from .pipeline import download, preanalyze, render

router = APIRouter(tags=["recut"])

# Contexte affiché de part et d'autre de la coupe courante : large pour
# voir la piste voisine (les concerts live n'ont pas de silences nets), mais
# borné pour que la tranche extraite reste légère (audio + waveform à servir).
# 15 s couvre confortablement les transitions parlées et applaudissements.
CONTEXT_PAD_S = 15.0


# --- Cas de figure ---------------------------------------------------------
def _track_kind(m: Manifest, track: dict) -> str:
    """« external » / « mono » / « multi » — quel chemin de recut appliquer."""
    if render.is_external(track):
        return "external"
    src = m.data.get("source", {}) or {}
    if src.get("clips"):
        return "multi"
    return "mono"


def _find_track(m: Manifest, n: int) -> dict:
    track = next((t for t in m.tracks if int(t.get("n", -1)) == n), None)
    if track is None:
        raise HTTPException(404, "piste introuvable")
    return track


def _master_wav_path(project_dir: Path, m: Manifest) -> Path:
    """Master WAV attendu par le pipeline ; 409 s'il a été purgé.

    Une piste concert/multi ne peut être re-coupée sans le master : c'est la
    seule source PCM lossless disponible. En rouvrant l'éditeur audio
    (`btn-open-editor` sur la fiche de gestion) le pipeline le re-télécharge
    tout seul — mieux vaut y renvoyer que dupliquer ici cette logique.
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
    sous la même racine mais chacun balaye ses propres traces (leur cycle de
    vie et leur nommage sont indépendants). Sans ça, une préparation externe
    (source re-téléchargée) laisserait son WAV indéfiniment.
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
    """MP3 128 k léger pour la lecture in-browser (calqué sur addtrack)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav), "-vn", "-c:a", "libmp3lame",
         "-b:a", "128k", str(out)],
        check=True, capture_output=True)


def _run_recut_prep_master(token: str, project_dir: Path, m: Manifest,
                           track: dict) -> None:
    """Prépare la waveform pour une piste concert/multi (source = master.wav).

    On extrait une tranche [track.start - PAD, track.end + PAD] du master
    et on en fait waveform + preview. Le front reçoit la durée de la
    tranche + la position des bornes actuelles dans cette tranche : le
    ClipTrimmer s'ouvre déjà callé sur la coupe existante, l'utilisateur
    n'a qu'à corriger.
    """
    work = _recut_dir(token)
    try:
        _job_set(token, state="running", stage="extract", pct=0.0)
        master = _master_wav_path(project_dir, m)
        master_dur = _wav_seconds(master)
        t_start = float(track.get("start") or 0.0)
        t_end = float(track.get("end") or master_dur)
        # Bornes de la tranche extraite : on empiète sur les voisins pour caler
        # visuellement la jonction (creux d'énergie, applaudissements).
        ext_start = max(0.0, t_start - CONTEXT_PAD_S)
        ext_end = min(master_dur, t_end + CONTEXT_PAD_S)
        slice_wav = work / "slice.wav"
        _extract_slice(master, ext_start, ext_end, slice_wav)

        _job_set(token, stage="waveform", pct=0.0)
        preanalyze.generate_waveform(slice_wav, work / "waveform.dat")

        _job_set(token, stage="preview", pct=0.0)
        _make_preview_mp3(slice_wav, work / "preview.mp3")

        # Durée exacte du WAV extrait : c'est elle qui pilote les poignées
        # (la borne demandée à ffmpeg peut être arrondie de quelques ms).
        duration = _wav_seconds(slice_wav)
        # Position des poignées actuelles DANS la tranche (0..duration).
        cur_start = max(0.0, t_start - ext_start)
        cur_end = min(duration, t_end - ext_start)
        # Le WAV ne sert plus (preview + waveform sont produits) : allégé,
        # au cas où l'application reste ouverte longtemps sur le token.
        slice_wav.unlink(missing_ok=True)
        _job_set(token, state="done", stage="done", pct=100.0,
                 duration=duration, cur_start=round(cur_start, 3),
                 cur_end=round(cur_end, 3),
                 ext_start=round(ext_start, 3), ext_end=round(ext_end, 3),
                 master_duration=round(master_dur, 3))
    except HTTPException:
        shutil.rmtree(work, ignore_errors=True)
        _job_set(token, state="error", error="master absent")
        raise
    except Exception as e:
        shutil.rmtree(work, ignore_errors=True)
        _job_set(token, state="error", error=str(e) or e.__class__.__name__)


def _run_recut_prep_external(token: str, url: str) -> None:
    """Fallback pour une piste externe : re-télécharge la source d'origine.

    Chemin identique à `addtrack._run_prep_bg` (mêmes noms de fichiers,
    même waveform + preview) — mais on ne peut pas juste appeler l'autre
    fonction car elle écrit sous `prep-{token}` alors qu'on veut
    `recut-{token}` (deux TTL indépendants, deux logiques de nettoyage).
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
        # `clip.wav` ne sert plus qu'à la préparation.
        wav.unlink(missing_ok=True)
        _job_set(token, state="done", stage="done", pct=100.0,
                 duration=duration)
    except Exception as e:
        shutil.rmtree(work, ignore_errors=True)
        _job_set(token, state="error", error=str(e) or e.__class__.__name__)


class RecutPrepIn(BaseModel):
    # Pas d'URL ni de payload : la piste + son manifest suffisent à savoir
    # quoi préparer. La signature reste explicite pour le versionnage OpenAPI.
    pass


@router.post("/api/albums/{slug}/tracks/{n}/recut/prep")
def recut_prep(slug: str, n: int,
               identity: dict = Depends(require_gestionnaire)) -> dict:
    """Prépare la waveform d'une piste existante pour la re-coupe.

    Le sondage/téléchargement (uniquement pour une piste externe) tourne en
    thread, comme `prep-clip` — le front sonde via l'endpoint status.
    """
    project_dir = PROJECTS_DIR / slug
    mpath = project_dir / "manifest.yaml"
    if not mpath.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(mpath)
    track = _find_track(m, n)

    _purge_jobs()
    addtrack._sweep_prep_dirs()  # partage le même volume (prep-*)
    _sweep_recut_dirs()          # + les préparations recut abandonnées
    token = uuid4().hex
    kind = _track_kind(m, track)
    _job_set(token, state="queued", stage="prep", pct=0.0,
             slug=slug, n=n, kind=kind)

    if kind == "external":
        url = ((track.get("source") or {}).get("url") or "").strip()
        if not url:
            raise HTTPException(
                400, "piste externe sans URL source (ré-upload requis)")
        threading.Thread(target=_run_recut_prep_external,
                         args=(token, url), daemon=True).start()
    else:
        # mono / multi : lecture du master local, pas de réseau. On délègue
        # tout de même à un thread : l'extraction ffmpeg peut prendre quelques
        # secondes sur une piste longue et bloquerait sinon la boucle FastAPI.
        threading.Thread(target=_run_recut_prep_master,
                         args=(token, project_dir, m, track),
                         daemon=True).start()
    return {"ok": True, "token": token, "kind": kind}


@router.get("/api/albums/{slug}/tracks/{n}/recut/{token}")
def recut_prep_status(slug: str, n: int, token: str,
                      identity: dict = Depends(require_gestionnaire)) -> dict:
    job = _job_get(token)
    if not job:
        raise HTTPException(404, "préparation inconnue")
    return {k: v for k, v in job.items() if k != "created_at"}


@router.get("/api/albums/{slug}/tracks/{n}/recut/{token}/waveform.dat")
def recut_prep_waveform(slug: str, n: int, token: str,
                        identity: dict = Depends(require_gestionnaire)):
    dat = _recut_dir(token) / "waveform.dat"
    if not dat.exists():
        raise HTTPException(404, "waveform indisponible")
    return FileResponse(str(dat), media_type="application/octet-stream")


@router.get("/api/albums/{slug}/tracks/{n}/recut/{token}/audio")
def recut_prep_audio(slug: str, n: int, token: str,
                     identity: dict = Depends(require_gestionnaire)):
    mp3 = _recut_dir(token) / "preview.mp3"
    if not mp3.exists():
        raise HTTPException(404, "aperçu indisponible")
    return FileResponse(str(mp3), media_type="audio/mpeg")


# --- Application de la nouvelle coupe --------------------------------------
class RecutApplyIn(BaseModel):
    token: str
    # Bornes IN THE PREPARED SLICE reference (0..duration renvoyée par prep).
    # Pour une piste externe : bornes dans la source complète (idem prep-clip).
    start: float
    end: float


def _rebuild_master_multi(project_dir: Path, m: Manifest, k_index: int,
                          seg_start: float, seg_end: float) -> tuple[float, list[float]]:
    """Re-assemble master.wav en remplaçant la k-ième tranche.

    Retourne (durée totale, durées des N tranches) pour recalculer les
    timecodes cumulés du manifest. Atomique : passage par un fichier
    temporaire puis `Path.replace` (comme `reorder_master`).
    """
    master = _master_wav_path(project_dir, m)
    source_dir = project_dir / "source"
    tracks = m.tracks
    segs: list[Path] = []
    durations: list[float] = []
    try:
        for i, t in enumerate(tracks):
            old_start = float(t.get("start") or 0.0)
            old_end = float(t.get("end") or 0.0)
            if i == k_index:
                # Sous-tranche demandée par l'utilisateur, exprimée en offset
                # relatif à l'ancienne tranche du master.
                new_start_master = old_start + seg_start
                new_end_master = old_start + seg_end
            else:
                new_start_master = old_start
                new_end_master = old_end
            seg = source_dir / f"recut_{i:02d}.wav"
            _extract_slice(master, new_start_master, new_end_master, seg)
            segs.append(seg)
            durations.append(_wav_seconds(seg))

        # Ré-assemblage lossless (formats déjà uniformes : PCM 44,1 kHz stéréo).
        tmp = source_dir / "master.recut.wav"
        inputs: list[str] = []
        for w in segs:
            inputs += ["-i", str(w)]
        n = len(segs)
        filt = ("".join(f"[{k}:a]" for k in range(n))
                + f"concat=n={n}:v=0:a=1[out]")
        subprocess.run(
            ["ffmpeg", "-y", *inputs, "-filter_complex", filt,
             "-map", "[out]", "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2",
             str(tmp)],
            check=True, capture_output=True)
        tmp.replace(master)
    finally:
        for s in segs:
            s.unlink(missing_ok=True)
    return sum(durations), durations


def _rerender_track_audio(project_dir: Path, m: Manifest, track: dict) -> Path:
    """Re-encode le MP3 d'une piste concert/multi depuis le master courant.

    Le nom de fichier suit `track_filename` : titre inchangé → même nom.
    L'ancien fichier est purgé si le nom a bougé (garde-fou renommage).
    """
    master = _master_wav_path(project_dir, m)
    audio_dir = project_dir / "build" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    expected = m.track_filename(track, "mp3")
    # Purge d'un éventuel ancien fichier de la même piste (titre modifié
    # entre-temps par l'éditeur) — sans quoi le bundle contiendrait les deux.
    for f in audio_dir.glob(f"{int(track['n']):02d}. *.mp3"):
        if f.name != expected:
            f.unlink(missing_ok=True)
    out = audio_dir / expected
    # `_render_or_cleanup` protège d'un fichier tronqué (SIGKILL, docker restart)
    # : on écrit d'abord dans `<nom>.mp3.part` puis on remplace atomiquement.
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
    slug = project_dir.name
    out = (project_dir / "build" / "audio"
           / m.track_filename(track, "mp3"))
    encode_track(src, out, seg_start, seg_end)
    # Persistance des nouvelles bornes dans le manifeste : sans ça, un
    # ré-encodage ultérieur repartirait des anciennes valeurs.
    src_meta = track.setdefault("source", {})
    src_meta["start"] = float(seg_start)
    src_meta["end"] = float(seg_end)
    src_meta["recut_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    m.save()
    return out


@router.post("/api/albums/{slug}/tracks/{n}/recut")
def recut_apply(slug: str, n: int, payload: RecutApplyIn,
                identity: dict = Depends(require_gestionnaire)) -> dict:
    """Applique la nouvelle coupe, ré-encode la piste, sauvegarde le manifeste.

    Le rendu est synchrone : re-couper une piste tient en quelques secondes
    (rognage PCM + encodage MP3 court), la latence est acceptable pour le
    front. Pas de file de rendu ; les jobs RQ sont réservés au rendu global.
    """
    project_dir = PROJECTS_DIR / slug
    mpath = project_dir / "manifest.yaml"
    if not mpath.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(mpath)
    track = _find_track(m, n)
    kind = _track_kind(m, track)

    if payload.end <= payload.start:
        raise HTTPException(400, "rognage : fin avant début")

    job = _job_get(payload.token)
    if not job or job.get("state") != "done" or int(job.get("n", -1)) != n:
        raise HTTPException(409, "préparation absente ou expirée")

    try:
        if kind == "external":
            out = _apply_recut_external(project_dir, m, track, payload.token,
                                        payload.start, payload.end)
            new_duration = payload.end - payload.start
            info: dict[str, Any] = {"file": out.name,
                                    "duration": round(new_duration, 3)}
        else:
            # Concert / multi : le token porte l'offset de la tranche extraite
            # dans le master (ext_start). Nouveau segment sur le master =
            # [ext_start + payload.start, ext_start + payload.end].
            ext_start = float(job.get("ext_start") or 0.0)
            master_dur = float(job.get("master_duration") or 0.0)
            new_start = ext_start + float(payload.start)
            new_end = ext_start + float(payload.end)
            if master_dur and new_end > master_dur + 0.01:
                raise HTTPException(400, "coupe hors master")
            if kind == "mono":
                # Ne touche PAS les pistes voisines : le geste est ponctuel.
                # Un chevauchement/gap avec la piste voisine n'est pas une
                # erreur (l'utilisateur peut vouloir empiéter sur un applau-
                # dissement), il est simplement resservi tel quel au rendu.
                track["start"] = round(new_start, 3)
                track["end"] = round(new_end, 3)
                m.save()
                out = _rerender_track_audio(project_dir, m, track)
                info = {"file": out.name, "start": track["start"],
                        "end": track["end"]}
            else:
                # multi-source : la nouvelle sous-tranche remplace la K-ième,
                # le master est ré-assemblé, les timecodes cumulés recalés.
                # Seul le MP3 de la piste K est ré-encodé (les autres pistes
                # gardent le même son, seuls leurs offsets bougent).
                k = next(i for i, t in enumerate(m.tracks)
                         if int(t.get("n", -1)) == n)
                _, seg_durations = _rebuild_master_multi(
                    project_dir, m, k, float(payload.start),
                    float(payload.end))
                t0 = 0.0
                for i, tr in enumerate(m.tracks):
                    dur = seg_durations[i]
                    tr["start"] = round(t0, 3)
                    tr["end"] = round(t0 + dur, 3)
                    t0 += dur
                m.data.setdefault("source", {})["duration"] = round(t0, 3)
                m.save()
                out = _rerender_track_audio(project_dir, m, track)
                info = {"file": out.name, "start": track["start"],
                        "end": track["end"], "total_duration": round(t0, 3)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"application impossible : {e}")

    # Dérivés album (waveform, preview.mp3) : régénération best-effort — c'est
    # ce que consulte l'éditeur audio à la prochaine ouverture, l'ancienne
    # forme d'onde ne refléterait plus le master ré-assemblé (cas multi).
    try:
        master = _master_wav_path(project_dir, m)
        preanalyze.generate_waveform(master,
                                     project_dir / "source" / "waveform.dat")
        (project_dir / "source" / "preview.mp3").unlink(missing_ok=True)
        from . import linktool
        linktool.make_preview(project_dir, m)
    except Exception:
        pass

    # Tags ID3 : le renommage n'est pertinent que si le titre a bougé (pas ici),
    # mais les tags durée changent — resynchronisation défensive et cheap.
    try:
        _write_track_tags(slug, m)
        _write_album_tags(slug, m)
    except Exception:
        pass

    # Jellyfin ne détecte pas seul un changement de contenu à l'intérieur
    # d'un album déjà monté (cf. addtrack, jellyfin.py) : refresh best-effort.
    try:
        jellyfin.refresh_album(slug)
    except Exception:
        pass

    # Nettoyage de la préparation consommée : la source (external) ou la
    # tranche (mono/multi) ne sert plus. Cohérent avec addtrack.from-url.
    shutil.rmtree(_recut_dir(payload.token), ignore_errors=True)
    with _JOBS_LOCK:
        _JOBS.pop(payload.token, None)

    return {"ok": True, "n": n, "kind": kind, **info}
