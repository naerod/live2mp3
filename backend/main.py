"""API FastAPI — formulaire, création de jobs, progression SSE, fichiers.

Accès prévu Tailscale only (allowlist Nginx en amont). Les jobs longs sont
délégués à RQ/Redis ; en l'absence de Redis (dev/test), exécution inline dans
un thread. La progression est publiée par projet et relue via SSE.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import jobs
from .manifest import PROJECTS_DIR, Manifest, new_manifest

BASE = Path(__file__).resolve().parent.parent
FRONTEND = BASE / "frontend"

app = FastAPI(title="live2mp3", docs_url="/api/docs")

# --- État de progression en mémoire (par slug) ----------------------------
_progress_bus: dict[str, "queue.Queue[dict]"] = {}
_progress_last: dict[str, list[dict]] = {}


def _publish(slug: str, event: dict) -> None:
    _progress_last.setdefault(slug, []).append(event)
    q = _progress_bus.get(slug)
    if q is not None:
        q.put(event)


def _make_cb(slug: str):
    def cb(stage: str, status: str, info: dict) -> None:
        _publish(slug, {"stage": stage, "status": status, "info": info,
                        "ts": time.time()})
    return cb


# --- Modèles --------------------------------------------------------------
class TrackIn(BaseModel):
    title: str
    n: int | None = None
    parts: list[str] | None = None
    start: float | None = None
    end: float | None = None
    locked: bool = False


class AlbumIn(BaseModel):
    artist: str
    title: str
    date: str | None = None
    venue: str | None = None
    festival: str | None = None
    cover: str | None = None


class JobIn(BaseModel):
    album: AlbumIn
    tracks: list[TrackIn]
    target: str = "data_disc"
    source_url: str = ""


# --- Routes ---------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    idx = FRONTEND / "index.html"
    if idx.exists():
        return HTMLResponse(idx.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>live2mp3</h1>")


@app.post("/api/jobs")
def create_job(job: JobIn) -> dict[str, Any]:
    m = new_manifest(
        job.album.model_dump(exclude_none=True),
        [t.model_dump(exclude_none=True) for t in job.tracks],
        target=job.target, source_url=job.source_url,
    )
    project_dir = PROJECTS_DIR / m.slug
    project_dir.mkdir(parents=True, exist_ok=True)
    m.save(project_dir / "manifest.yaml")
    return {"slug": m.slug, "manifest": str(project_dir / "manifest.yaml")}


@app.get("/api/jobs/{slug}/manifest")
def get_manifest(slug: str) -> dict:
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "projet introuvable")
    return Manifest.load(path).data


@app.put("/api/jobs/{slug}/markers")
def update_markers(slug: str, payload: dict) -> dict:
    """Écrit les timecodes validés depuis l'UI Peaks.js et verrouille."""
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "projet introuvable")
    m = Manifest.load(path)
    markers = {int(k): v for k, v in payload.get("tracks", {}).items()}
    for track in m.tracks:
        if track["n"] in markers:
            track["start"] = float(markers[track["n"]]["start"])
            track["end"] = float(markers[track["n"]]["end"])
    if payload.get("lock", True):
        m.lock_all()
    else:
        m.save()
    return {"ok": True, "tracks": len(m.tracks)}


def _run_render_bg(slug: str, media: str, gap: float) -> None:
    project_dir = PROJECTS_DIR / slug
    try:
        jobs.run_render_pipeline(project_dir, media=media, gap_seconds=gap,
                                 progress=_make_cb(slug))
        _publish(slug, {"stage": "all", "status": "complete", "info": {},
                        "ts": time.time()})
    except Exception as e:  # pragma: no cover
        _publish(slug, {"stage": "error", "status": "error",
                        "info": {"message": str(e)}, "ts": time.time()})


@app.post("/api/jobs/{slug}/render")
def start_render(slug: str, media: str = "audio", gap: float = 2.0) -> dict:
    project_dir = PROJECTS_DIR / slug
    if not (project_dir / "manifest.yaml").exists():
        raise HTTPException(404, "projet introuvable")
    _progress_last[slug] = []
    _progress_bus[slug] = queue.Queue()
    threading.Thread(target=_run_render_bg, args=(slug, media, gap),
                     daemon=True).start()
    return {"ok": True, "slug": slug}


@app.get("/api/jobs/{slug}/events")
def events(slug: str) -> StreamingResponse:
    """Flux SSE de progression."""
    def gen():
        for ev in _progress_last.get(slug, []):
            yield f"data: {json.dumps(ev)}\n\n"
        q = _progress_bus.get(slug)
        if q is None:
            return
        while True:
            try:
                ev = q.get(timeout=30)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            yield f"data: {json.dumps(ev)}\n\n"
            if ev.get("status") in ("complete", "error"):
                break
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/jobs/{slug}/bundle")
def download_bundle(slug: str) -> FileResponse:
    path = PROJECTS_DIR / slug / "build" / "bundle.zip"
    if not path.exists():
        raise HTTPException(404, "bundle non généré")
    return FileResponse(path, filename=f"{slug}.zip",
                        media_type="application/zip")


@app.get("/api/jobs/{slug}/waveform.dat")
def waveform(slug: str) -> FileResponse:
    path = PROJECTS_DIR / slug / "source" / "waveform.dat"
    if not path.exists():
        raise HTTPException(404, "waveform absente")
    return FileResponse(path, media_type="application/octet-stream")


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
