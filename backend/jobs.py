"""Orchestration du pipeline : exécution stage par stage avec progression.

Chaque stage est idempotent (skip si `done`). La progression est publiée via
un callback (branché sur SSE côté API, ou Redis pub/sub côté worker).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from .pipeline import artwork, disc, preanalyze, render, tags

log = logging.getLogger(__name__)

ProgressCb = Callable[[str, str, dict], None]

# Ordre d'exécution (download/preanalyze pilotés à part car réseau/GPU)
RENDER_STAGES = ["render", "tags", "artwork", "disc"]


def _noop(stage: str, status: str, info: dict) -> None:  # pragma: no cover
    pass


def run_render_pipeline(project_dir: str | Path, *, media: str = "audio",
                        video: bool = True, gap_seconds: float = 2.0,
                        force: bool = False,
                        progress: ProgressCb = _noop,
                        cancel: Callable[[], bool] | None = None,
                        paused: Callable[[], bool] | None = None) -> dict:
    """Exécute render -> tags -> artwork -> disc.

    Suppose les timecodes déjà présents (validés via l'UI Peaks.js).
    `force` : ré-encode même les pistes dont le fichier existe déjà — requis
    pour un re-rendu sur un album déjà publié (sinon une piste dont le nom ne
    change pas garderait son ancien découpage, cf. render.run/idempotence).
    `cancel` : sondé pendant le rendu et entre les stages ; lève
    render.Cancelled dès qu'il passe à True.
    """
    project_dir = Path(project_dir)
    results: dict = {}

    def _check() -> None:
        # Les stages après render durent quelques secondes : inutile de les
        # interrompre en plein milieu, il suffit de ne pas enchaîner.
        if cancel and cancel():
            raise render.Cancelled()

    progress("render", "running", {})

    def _on_track(done: int, total: int, title: str) -> None:
        progress("render", "running", {
            "done": done, "total": total, "title": title,
            "pct": (done / total * 100) if total else 0,
        })

    results["render"] = render.run(project_dir, video=video, force=force,
                                   on_track=_on_track, cancel=cancel,
                                   paused=paused)
    progress("render", "done", {"tracks": len(results["render"]["audio"])})
    _check()

    progress("tags", "running", {})
    results["tags"] = tags.run(project_dir)
    progress("tags", "done", {})

    progress("artwork", "running", {})
    results["artwork"] = artwork.run(project_dir)
    progress("artwork", "done", {})

    progress("disc", "running", {})
    results["disc"] = disc.run(project_dir, media=media, gap_seconds=gap_seconds)
    progress("disc", "done", {})

    # Le ZIP n'est plus construit ici : il ne compresse rien (MP3/MP4 déjà
    # compressés) et dupliquait donc intégralement build/ sur le disque —
    # 13 Go de doublons relevés sur la bibliothèque le 2026-07-30. Il est
    # désormais assemblé à la volée au téléchargement (cf. main.py).

    return results


def run_video_pipeline(project_dir: str | Path, *, force: bool = False,
                       progress: ProgressCb = _noop,
                       cancel: Callable[[], bool] | None = None,
                       paused: Callable[[], bool] | None = None) -> dict:
    """Phase 2 : le seul rendu MP4, sans retoucher à l'audio déjà produit.

    Émet une progression **mesurée** (ffmpeg `-progress`), là où l'ancien
    pipeline ne signalait que « début » et « fin » d'un ré-encodage de 15 min.
    """
    project_dir = Path(project_dir)
    progress("video", "running", {"pct": 0})

    def _on_video(frac: float) -> None:
        progress("video", "running", {"pct": round(frac * 100, 1)})

    out = render.run(project_dir, video=True, audio=False, force=force,
                     on_video=_on_video, cancel=cancel, paused=paused)
    progress("video", "done", {"pct": 100, "files": len(out["video"])})
    return {"render": out}


def render_job(*, slug: str, media: str, gap: float, video: bool,
               republish: bool) -> dict:
    """Phase 1 : audio, métadonnées, pochettes, image disque — puis mise en
    file du rendu vidéo (phase 2) s'il y a lieu.

    Découplage du 2026-08-03 : la vidéo était rendue dans ce même job, si bien
    qu'un album audio prêt en 3 minutes restait invisible pendant les 15 à
    20 minutes du ré-encodage x264. L'album est désormais livré dès la fin de
    la phase 1 ; la vidéo le rejoint plus tard, sans bloquer personne.

    Vit dans le worker, pas dans l'API : la progression passe donc par Redis,
    et l'annulation/pause sont lues depuis Redis à chaque sondage.
    """
    from pathlib import Path as _Path

    from . import jellyfin, notifications, progress, renderqueue
    from .manifest import PROJECTS_DIR

    project_dir = _Path(PROJECTS_DIR) / slug
    requested_by = renderqueue.get_meta(slug).get("requested_by", "")
    cb = progress.make_cb(slug)
    try:
        run_render_pipeline(
            project_dir, media=media, gap_seconds=gap, video=False,
            force=republish, progress=cb,
            cancel=lambda: renderqueue.cancel_requested(slug),
            paused=lambda: renderqueue.is_paused(slug),
        )
        if republish:
            # Un album déjà publié qu'on vient de re-rendre doit réapparaître à
            # jour dans Jellyfin : le symlink existe déjà, sync-media.sh ne voit
            # pas un changement de contenu interne.
            jellyfin.refresh_library()
        # Purge des masters : désactivée par défaut. Elle rend ~1,6 Go par
        # album mais impose un re-téléchargement (2-3 min) à la prochaine
        # ré-ouverture de l'éditeur — et devient définitive si la vidéo source
        # disparaît de YouTube entre-temps. À n'activer qu'en connaissance de
        # cause, via L2M_PURGE_MASTERS=1.
        if os.environ.get("L2M_PURGE_MASTERS") == "1":
            try:
                from .pipeline import download as _dl
                _dl.purge_master(project_dir)
            except Exception:
                pass
        try:
            notifications.notify_render_done(slug, requested_by)
        except Exception:
            # Une notification ratée ne doit pas faire échouer un rendu abouti.
            pass
        # Phase 2 : le MP4 part dans son propre job, derrière celui-ci. La
        # mise en file précède l'évènement « complete » pour que le client qui
        # bascule aussitôt sur l'écran final trouve déjà le rendu vidéo dans
        # `/api/render-queue` — sans quoi il n'afficherait rien à suivre.
        video_queued = False
        if video and _has_video_source(project_dir):
            try:
                renderqueue.enqueue_video(slug, republish=republish,
                                          requested_by=requested_by)
                video_queued = True
            except Exception as exc:
                # L'album audio est livré : un échec de mise en file de la
                # vidéo se signale sans faire échouer la phase 1.
                log.warning("mise en file du rendu vidéo %s impossible : %s",
                            slug, exc)
        progress.publish(slug, {"stage": "all", "status": "complete",
                                "info": {"video_queued": video_queued}})
        return {"ok": True, "slug": slug, "video_queued": video_queued}
    except render.Cancelled:
        progress.publish(slug, {"stage": "all", "status": "cancelled",
                                "info": {}})
        return {"ok": False, "slug": slug, "cancelled": True}
    except Exception as e:
        progress.publish(slug, {"stage": "error", "status": "error",
                                "info": {"message": str(e)}})
        raise
    finally:
        renderqueue.clear_cancel(slug)
        renderqueue.set_paused(slug, False)
        renderqueue.clear_meta(slug)


def render_video_job(*, slug: str, republish: bool) -> dict:
    """Phase 2 exécutée par le worker RQ (cf. renderqueue.enqueue_video).

    L'album audio est déjà en place : cet encodage n'a plus personne devant
    lui, il publie donc son avancement réel pour la file de rendu et notifie
    séparément à la fin.
    """
    from pathlib import Path as _Path

    from . import jellyfin, notifications, progress, renderqueue
    from .manifest import PROJECTS_DIR

    KIND = renderqueue.KIND_VIDEO
    project_dir = _Path(PROJECTS_DIR) / slug
    requested_by = renderqueue.get_meta(slug, KIND).get("requested_by", "")

    def cb(stage: str, status: str, info: dict) -> None:
        # Double canal : le pub/sub pour un client resté sur l'écran de
        # progression, et une clé simple pour `/api/render-queue`, que la page
        # des brouillons interroge sans maintenir de flux ouvert.
        progress.publish(slug, {"stage": stage, "status": status, "info": info})
        if "pct" in info:
            renderqueue.set_pct(slug, float(info["pct"]), KIND)

    try:
        run_video_pipeline(
            project_dir, force=republish, progress=cb,
            cancel=lambda: renderqueue.cancel_requested(slug, KIND),
            paused=lambda: renderqueue.is_paused(slug, KIND),
        )
        if republish:
            # Même raison que pour l'audio : sync-media.sh ne voit pas un
            # changement de contenu dans un dossier déjà monté.
            jellyfin.refresh_library()
        try:
            notifications.notify_video_done(slug, requested_by)
        except Exception:
            pass
        progress.publish(slug, {"stage": "video_all", "status": "complete",
                                "info": {}})
        return {"ok": True, "slug": slug}
    except render.Cancelled:
        progress.publish(slug, {"stage": "video_all", "status": "cancelled",
                                "info": {}})
        return {"ok": False, "slug": slug, "cancelled": True}
    except Exception as e:
        progress.publish(slug, {"stage": "video_error", "status": "error",
                                "info": {"message": str(e)}})
        raise
    finally:
        renderqueue.clear_cancel(slug, KIND)
        renderqueue.set_paused(slug, False, KIND)
        renderqueue.clear_pct(slug, KIND)
        renderqueue.clear_meta(slug, KIND)


def _has_video_source(project_dir: Path) -> bool:
    """Y a-t-il de quoi rendre une vidéo ? Inutile d'enfiler un job qui
    n'aurait rien à encoder (import audio-only, master vidéo purgé)."""
    try:
        from .manifest import Manifest
        m = Manifest.load(Path(project_dir) / "manifest.yaml")
    except Exception:
        return False
    mkv = Path(project_dir) / m.data.get("source", {}).get("master_mkv", "")
    has_tracks = any(t.get("start") is not None and t.get("end") is not None
                     for t in m.tracks)
    return mkv.is_file() and has_tracks


def run_full_pipeline(project_dir: str | Path, *, download_fn=None,
                      call_llm: bool = True, media: str = "audio",
                      video: bool = True, gap_seconds: float = 2.0,
                      progress: ProgressCb = _noop) -> dict:
    """Pipeline complet : (download) -> preanalyze -> render pipeline.

    `download_fn` est injecté (yt-dlp) ; None => on suppose master déjà présent
    (respect du point d'arrêt D : aucun téléchargement non demandé).
    """
    project_dir = Path(project_dir)
    results: dict = {}

    if download_fn is not None:
        progress("download", "running", {})
        results["download"] = download_fn(project_dir)
        progress("download", "done", {})

    progress("ai_markers", "running", {})
    results["preanalyze"] = preanalyze.run(project_dir, call_llm=call_llm)
    progress("ai_markers", "done", results["preanalyze"])

    results.update(run_render_pipeline(
        project_dir, media=media, video=video,
        gap_seconds=gap_seconds, progress=progress))
    return results
