"""Orchestration du pipeline : exécution stage par stage avec progression.

Chaque stage est idempotent (skip si `done`). La progression est publiée via
un callback (branché sur SSE côté API, ou Redis pub/sub côté worker).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .manifest import Manifest
from .pipeline import artwork, bundle, disc, preanalyze, render, tags

ProgressCb = Callable[[str, str, dict], None]

# Ordre d'exécution (download/preanalyze pilotés à part car réseau/GPU)
RENDER_STAGES = ["render", "tags", "artwork", "disc", "bundle"]


def _noop(stage: str, status: str, info: dict) -> None:  # pragma: no cover
    pass


def run_render_pipeline(project_dir: str | Path, *, media: str = "audio",
                        video: bool = True, gap_seconds: float = 2.0,
                        force: bool = False,
                        progress: ProgressCb = _noop) -> dict:
    """Exécute render -> tags -> artwork -> disc -> bundle.

    Suppose les timecodes déjà présents (validés via l'UI Peaks.js).
    `force` : ré-encode même les pistes dont le fichier existe déjà — requis
    pour un re-rendu sur un album déjà publié (sinon une piste dont le nom ne
    change pas garderait son ancien découpage, cf. render.run/idempotence).
    """
    project_dir = Path(project_dir)
    results: dict = {}

    progress("render", "running", {})
    results["render"] = render.run(project_dir, video=video, force=force)
    progress("render", "done", {"tracks": len(results["render"]["audio"])})

    progress("tags", "running", {})
    results["tags"] = tags.run(project_dir)
    progress("tags", "done", {})

    progress("artwork", "running", {})
    results["artwork"] = artwork.run(project_dir)
    progress("artwork", "done", {})

    progress("disc", "running", {})
    results["disc"] = disc.run(project_dir, media=media, gap_seconds=gap_seconds)
    progress("disc", "done", {})

    progress("bundle", "running", {})
    results["bundle"] = bundle.run(project_dir)
    progress("bundle", "done", {"count": results["bundle"]["count"]})

    return results


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
