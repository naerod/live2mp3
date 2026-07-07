"""API FastAPI — formulaire, création de jobs, progression SSE, fichiers.

Accès prévu Tailscale only (allowlist Nginx en amont). Les jobs longs sont
délégués à RQ/Redis ; en l'absence de Redis (dev/test), exécution inline dans
un thread. La progression est publiée par projet et relue via SSE.
"""
from __future__ import annotations

import io
import json
import queue
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import catalogue, jobs
from .auth import require_gestionnaire, require_user, roles
from .manifest import PROJECTS_DIR, Manifest, new_manifest

# Labels dérivés automatiquement de la disponibilité média (non éditables).
DERIVED_LABELS = {"audio", "vidéo", "video", "audio + vidéo", "audio + video"}

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


# --- Santé (public, GET + HEAD pour le hub) -------------------------------
@app.api_route("/healthz", methods=["GET", "HEAD"])
def healthz() -> dict:
    return {"ok": True}


# --- Routes publiques (vitrine) -------------------------------------------
@app.get("/", response_class=HTMLResponse)
def vitrine() -> HTMLResponse:
    page = FRONTEND / "vitrine.html"
    if page.exists():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>live2mp3</h1>")


@app.get("/api/catalogue")
def get_catalogue() -> list[dict]:
    """Liste publique des albums disponibles."""
    return catalogue.list_albums()


@app.get("/api/me")
def me(identity: dict = Depends(roles)) -> dict:
    """État de connexion + rôles (soft-auth via nginx). Jamais d'erreur."""
    return identity


def _track_file(project_dir: Path, n: int) -> Path | None:
    """Retrouve le MP3 d'une piste (préfixe numéro : '01_…' ou '01 - …')."""
    src = project_dir / "build" / "audio"
    if not src.exists():
        return None
    import re
    pat = re.compile(rf"^0*{int(n)}(?=\D)")
    for f in sorted(src.glob("*.mp3")):
        if pat.match(f.name):
            return f
    return None


@app.get("/api/catalogue/{slug}")
def catalogue_detail(slug: str) -> dict:
    """Fiche publique d'un album : métadonnées, labels, setlist (titres)."""
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(path)
    project_dir = PROJECTS_DIR / slug
    cat = {a["slug"]: a for a in catalogue.list_albums()}.get(slug, {})
    tracks = []
    for t in m.tracks:
        tracks.append({
            "n": t.get("n"),
            "title": t.get("title"),
            "dl": _track_file(project_dir, t.get("n")) is not None,
        })
    return {
        "slug": slug,
        "album": m.data.get("album", {}),
        "labels": cat.get("labels", []),
        "has_cover": cat.get("has_cover", False),
        "has_traycard": cat.get("has_traycard", False),
        "has_mp3": cat.get("has_mp3", False),
        "has_mp4": cat.get("has_mp4", False),
        "tracks": tracks,
    }


@app.get("/cover/{slug}")
def get_cover(slug: str) -> FileResponse:
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(path)
    cover_rel = m.data.get("album", {}).get("cover")
    if not cover_rel:
        raise HTTPException(404, "pas de pochette")
    cover = PROJECTS_DIR / slug / cover_rel
    if not cover.exists():
        raise HTTPException(404, "pochette absente")
    return FileResponse(cover)


# --- Téléchargements (niveau user) ----------------------------------------
def _zip_media(project_dir: Path, kind: str) -> Path:
    """Construit (et met en cache) un zip des MP3 ou MP4 d'un album."""
    sub = "audio" if kind == "mp3" else "video"
    src = project_dir / "build" / sub
    if not src.exists() or not any(src.glob(f"*.{kind}")):
        raise HTTPException(404, f"aucun {kind} pour cet album")
    out = project_dir / "build" / f"download_{kind}.zip"
    newest = max((f.stat().st_mtime for f in src.glob(f"*.{kind}")), default=0)
    if not out.exists() or out.stat().st_mtime < newest:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(src.glob(f"*.{kind}")):
                z.write(f, f.name)
    return out


# Routes spécifiques AVANT la route générique {kind} (sinon "cover" y matche).
@app.get("/download/{slug}/track/{n}")
def download_track(slug: str, n: int,
                   identity: dict = Depends(require_user)) -> FileResponse:
    project_dir = PROJECTS_DIR / slug
    if not (project_dir / "manifest.yaml").exists():
        raise HTTPException(404, "album introuvable")
    f = _track_file(project_dir, n)
    if not f:
        raise HTTPException(404, "piste introuvable")
    return FileResponse(f, filename=f.name, media_type="audio/mpeg")


@app.get("/download/{slug}/cover")
def download_cover(slug: str, identity: dict = Depends(require_user)) -> FileResponse:
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    cover_rel = Manifest.load(path).data.get("album", {}).get("cover")
    cover = PROJECTS_DIR / slug / cover_rel if cover_rel else None
    if not cover or not cover.exists():
        raise HTTPException(404, "pas de pochette")
    return FileResponse(cover, filename=f"{slug}-cover{cover.suffix}")


@app.get("/download/{slug}/traycard")
def download_traycard(slug: str, identity: dict = Depends(require_user)) -> FileResponse:
    tc = PROJECTS_DIR / slug / "artwork" / "tray_card.pdf"
    if not tc.exists():
        raise HTTPException(404, "pas de tray card")
    return FileResponse(tc, filename=f"{slug}-traycard.pdf",
                        media_type="application/pdf")


@app.get("/download/{slug}/{kind}")
def download_media(slug: str, kind: str,
                   identity: dict = Depends(require_user)) -> FileResponse:
    if kind not in ("mp3", "mp4"):
        raise HTTPException(400, "type invalide (mp3|mp4)")
    project_dir = PROJECTS_DIR / slug
    if not (project_dir / "manifest.yaml").exists():
        raise HTTPException(404, "album introuvable")
    zip_path = _zip_media(project_dir, kind)
    return FileResponse(zip_path, filename=f"{slug}-{kind}.zip",
                        media_type="application/zip")


# --- Gestion d'album (niveau gestionnaire) --------------------------------
@app.get("/api/albums/{slug}")
def album_detail(slug: str,
                 identity: dict = Depends(require_gestionnaire)) -> dict:
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(path)
    album = m.data.get("album", {})
    cat = {a["slug"]: a for a in catalogue.list_albums()}.get(slug, {})
    return {
        "slug": slug,
        "album": album,
        "labels": list(album.get("labels", []) or []),
        "derived_labels": [l for l in cat.get("labels", []) if l in DERIVED_LABELS],
        "all_labels": catalogue.all_labels(),
        "has_cover": cat.get("has_cover", False),
        "has_traycard": cat.get("has_traycard", False),
        "has_mp3": cat.get("has_mp3", False),
        "has_mp4": cat.get("has_mp4", False),
        "tracks": [{"n": t.get("n"), "title": t.get("title")} for t in m.tracks],
    }


@app.put("/api/albums/{slug}/labels")
def update_labels(slug: str, payload: dict,
                  identity: dict = Depends(require_gestionnaire)) -> dict:
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(path)
    seen, clean = set(), []
    for raw in payload.get("labels", []):
        l = str(raw).strip()
        if not l or l.lower() in DERIVED_LABELS:   # les dérivés ne se stockent pas
            continue
        if l.lower() not in seen:
            seen.add(l.lower())
            clean.append(l)
    m.data.setdefault("album", {})["labels"] = clean
    m.save()
    return {"ok": True, "labels": clean}


# --- Outil (niveau gestionnaire) ------------------------------------------
@app.get("/app", response_class=HTMLResponse)
def tool(identity: dict = Depends(require_gestionnaire)) -> HTMLResponse:
    idx = FRONTEND / "index.html"
    if idx.exists():
        return HTMLResponse(idx.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>live2mp3 — outil</h1>")


@app.get("/app/album/{slug}", response_class=HTMLResponse)
def album_admin_page(slug: str,
                     identity: dict = Depends(require_gestionnaire)) -> HTMLResponse:
    page = FRONTEND / "album.html"
    if page.exists():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>live2mp3 — gestion album</h1>")


@app.post("/api/jobs")
def create_job(job: JobIn,
               identity: dict = Depends(require_gestionnaire)) -> dict[str, Any]:
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
def get_manifest(slug: str,
                 identity: dict = Depends(require_gestionnaire)) -> dict:
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "projet introuvable")
    return Manifest.load(path).data


@app.put("/api/jobs/{slug}/markers")
def update_markers(slug: str, payload: dict,
                   identity: dict = Depends(require_gestionnaire)) -> dict:
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
def start_render(slug: str, media: str = "audio", gap: float = 2.0,
                 identity: dict = Depends(require_gestionnaire)) -> dict:
    project_dir = PROJECTS_DIR / slug
    if not (project_dir / "manifest.yaml").exists():
        raise HTTPException(404, "projet introuvable")
    _progress_last[slug] = []
    _progress_bus[slug] = queue.Queue()
    threading.Thread(target=_run_render_bg, args=(slug, media, gap),
                     daemon=True).start()
    return {"ok": True, "slug": slug}


@app.get("/api/jobs/{slug}/events")
def events(slug: str,
           identity: dict = Depends(require_gestionnaire)) -> StreamingResponse:
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
def download_bundle(slug: str,
                    identity: dict = Depends(require_gestionnaire)) -> FileResponse:
    path = PROJECTS_DIR / slug / "build" / "bundle.zip"
    if not path.exists():
        raise HTTPException(404, "bundle non généré")
    return FileResponse(path, filename=f"{slug}.zip",
                        media_type="application/zip")


@app.get("/api/jobs/{slug}/waveform.dat")
def waveform(slug: str,
             identity: dict = Depends(require_gestionnaire)) -> FileResponse:
    path = PROJECTS_DIR / slug / "source" / "waveform.dat"
    if not path.exists():
        raise HTTPException(404, "waveform absente")
    return FileResponse(path, media_type="application/octet-stream")


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
