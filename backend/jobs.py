"""Orchestration du pipeline : exécution stage par stage avec progression.

Chaque stage est idempotent (skip si `done`). La progression est publiée via
un callback (branché sur SSE côté API, ou Redis pub/sub côté worker).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from .manifest import Manifest
from .pipeline import artwork, disc, preanalyze, render, tags

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


def render_job(*, slug: str, media: str, gap: float, video: bool,
               republish: bool) -> dict:
    """Point d'entrée exécuté par le worker RQ (cf. renderqueue.enqueue).

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
            project_dir, media=media, gap_seconds=gap, video=video,
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
        progress.publish(slug, {"stage": "all", "status": "complete", "info": {}})
        return {"ok": True, "slug": slug}
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
