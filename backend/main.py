"""API FastAPI — formulaire, création de jobs, progression SSE, fichiers.

Accès prévu Tailscale only (allowlist Nginx en amont). Les jobs longs sont
délégués à RQ/Redis ; en l'absence de Redis (dev/test), exécution inline dans
un thread. La progression est publiée par projet et relue via SSE.
"""
from __future__ import annotations

import hashlib
import io
import json
import queue
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import re

from mutagen.mp3 import MP3
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC, ID3NoHeaderError
from . import catalogue, jobs
from .albumfiles import (
    _extract_embedded_cover,
    _file_track_n,
    _rename_audio_files,
    _sanitize_filename,
    _write_album_cover,
    _write_album_tags,
)
from .auth import require_gestionnaire, require_user, roles
from .db import get_conn, init_db
from .import_album import router as import_router
from .manifest import PROJECTS_DIR, Manifest, new_manifest
from .covers import (
    MEDIA_TYPES,
    cover_file,
    rank_covers,
    router as covers_router,
    top_cover,
    traycard_file,
    zip_basename,
)
from .printable import cover_pdf, traycard_pdf
from .social import router as social_router

# Labels dérivés automatiquement de la disponibilité média (non éditables).
DERIVED_LABELS = {"audio", "vidéo", "video", "audio + vidéo", "audio + video"}

BASE = Path(__file__).resolve().parent.parent
FRONTEND = BASE / "frontend"

app = FastAPI(title="live2mp3", docs_url="/api/docs")

# Système social (profils, favoris, commentaires) — routes /api/social, /u, /avatar.
app.include_router(social_router)
app.include_router(covers_router)

# Import d'un album prêt (dépôt de MP3 ou ZIP) — routes /api/import/*.
app.include_router(import_router)


@app.on_event("startup")
def _startup() -> None:
    init_db()

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


class AlbumMetaIn(BaseModel):
    artist: str
    title: str
    date: str | None = None
    venue: str | None = None
    festival: str | None = None
    source_url: str = ""
    source_label: str = ""


class TrackEditIn(BaseModel):
    n: int
    title: str


class TracksEditIn(BaseModel):
    tracks: list[TrackEditIn]


class PublishIn(BaseModel):
    published: bool


# --- Santé & version (public) ---------------------------------------------
import os

APP_ENV = os.environ.get("APP_ENV", "prod")
GIT_COMMIT = os.environ.get("GIT_COMMIT", "")[:7]
_vfile = BASE / "VERSION"
APP_VERSION = _vfile.read_text().strip() if _vfile.exists() else "0.0.0"


@app.api_route("/healthz", methods=["GET", "HEAD"])
def healthz() -> dict:
    return {"ok": True}


@app.get("/api/version")
def version() -> dict:
    return {"version": APP_VERSION, "env": APP_ENV, "commit": GIT_COMMIT}


# --- Routes publiques (vitrine) -------------------------------------------
@app.get("/", response_class=HTMLResponse)
def vitrine() -> HTMLResponse:
    page = FRONTEND / "vitrine.html"
    if page.exists():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>live2mp3</h1>")


@app.get("/api/catalogue")
def get_catalogue(sort: str = "date_concert", identity: dict = Depends(roles)) -> list[dict]:
    """Liste des albums. Gestionnaires voient les brouillons, public non."""
    include_drafts = identity.get("is_gestionnaire", False)
    return catalogue.list_albums(sort=sort, include_drafts=include_drafts)


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
    meta = m.data.get("meta", {})
    src = m.data.get("source", {})
    return {
        "slug": slug,
        "album": m.data.get("album", {}),
        "labels": cat.get("labels", []),
        "has_cover": cat.get("has_cover", False),
        "has_traycard": cat.get("has_traycard", False),
        "has_mp3": cat.get("has_mp3", False),
        "has_mp4": cat.get("has_mp4", False),
        "tracks": tracks,
        "imported_by": meta.get("imported_by", ""),
        "imported_at": meta.get("imported_at", ""),
        "source_url": src.get("url", "") or "",
        "source_label": src.get("label", "") or "",
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
def _album_extras(project_dir: Path) -> list[tuple[Path, str]]:
    """Toutes les pochettes de l'album + leurs tray cards, pour le zip complet.

    Nommage `artwork/<rang>-<auteur>_cover.ext` / `_traycard.ext` : le rang vient
    du classement (gagnante en 01), donc le tri alphabétique de l'archive
    reproduit l'ordre de popularité et colle chaque tray card à sa cover, seul le
    suffixe les séparant.

    Retourne des couples (chemin source, nom dans l'archive).
    """
    slug = project_dir.name
    extras: list[tuple[Path, str]] = []
    with get_conn() as conn:
        rows = rank_covers(conn, slug)
    for i, r in enumerate(rows):
        base = zip_basename(i + 1, r["username"])
        cpath = cover_file(slug, r["file_key"], r["cover_ext"])
        if cpath.exists():
            extras.append((cpath, f"artwork/{base}_cover{r['cover_ext']}"))
        if r["traycard_ext"]:
            tpath = traycard_file(slug, r["file_key"], r["traycard_ext"])
            if tpath.exists():
                extras.append((tpath, f"artwork/{base}_traycard{r['traycard_ext']}"))
    return extras


def _zip_media(project_dir: Path, kind: str) -> Path:
    """Construit (et met en cache) un zip des MP3/MP4 + toutes les pochettes."""
    sub = "audio" if kind == "mp3" else "video"
    src = project_dir / "build" / sub
    if not src.exists() or not any(src.glob(f"*.{kind}")):
        raise HTTPException(404, f"aucun {kind} pour cet album")
    out = project_dir / "build" / f"download_{kind}.zip"

    # L'APIC suit la pochette gagnante, mais on ne la recalcule qu'ici : la
    # réécrire à chaque like retoucherait tout l'album pour un changement que
    # personne ne télécharge. No-op si l'image embarquée est déjà la bonne.
    mpath = project_dir / "manifest.yaml"
    if kind == "mp3" and mpath.exists():
        _write_album_cover(project_dir.name, Manifest.load(mpath))

    extras = _album_extras(project_dir)
    # Le cache est invalidé si un média OU un fichier annexe est plus récent que le zip.
    mtimes = [f.stat().st_mtime for f in src.glob(f"*.{kind}")]
    mtimes += [p.stat().st_mtime for p, _ in extras]
    newest = max(mtimes, default=0)
    # ...mais un changement de classement renomme les entrées sans toucher aucun
    # mtime. On grave la liste des noms dans le commentaire du zip pour détecter
    # ce cas, que les dates seules manqueraient.
    layout = hashlib.sha1(
        "|".join(a for _, a in extras).encode()
    ).hexdigest().encode()
    stale = True
    if out.exists() and out.stat().st_mtime >= newest:
        try:
            with zipfile.ZipFile(out) as z:
                stale = z.comment != layout
        except (zipfile.BadZipFile, OSError):
            stale = True
    if stale:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.comment = layout
            for f in sorted(src.glob(f"*.{kind}")):
                z.write(f, f.name)
            for p, arcname in extras:
                z.write(p, arcname)
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


def _album_traycard(slug: str) -> tuple[Path, str]:
    """Tray card de la pochette gagnante.

    Une tray card n'existe que portée par une cover : c'est donc la gagnante qui
    décide, et un album dont la gagnante n'en a pas n'en propose pas — même si
    une pochette moins bien classée en a une (elle reste servie par son id).
    """
    with get_conn() as conn:
        win = top_cover(conn, slug)
    if not win or not win["traycard_ext"]:
        raise HTTPException(404, "pas de tray card")
    ext = win["traycard_ext"]
    tc = traycard_file(slug, win["file_key"], ext)
    if not tc.exists():
        raise HTTPException(404, "pas de tray card")
    return tc, ext


@app.get("/download/{slug}/traycard")
def download_traycard(slug: str, identity: dict = Depends(require_user)) -> FileResponse:
    tc, ext = _album_traycard(slug)
    return FileResponse(tc, filename=f"{slug}-traycard{ext}",
                        media_type=MEDIA_TYPES.get(ext, "application/pdf"))


@app.get("/app/traycard/{slug}")
def view_traycard(slug: str, identity: dict = Depends(require_gestionnaire)) -> FileResponse:
    """Même fichier, servi inline pour la prévisualisation en iframe."""
    tc, ext = _album_traycard(slug)
    return FileResponse(tc, media_type=MEDIA_TYPES.get(ext, "application/pdf"),
                        content_disposition_type="inline")


from fastapi.responses import Response as _Response


@app.get("/download/{slug}/cover/pdf")
def download_cover_printable(slug: str,
                             identity: dict = Depends(require_user)) -> _Response:
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    cover_rel = Manifest.load(path).data.get("album", {}).get("cover")
    cover = PROJECTS_DIR / slug / cover_rel if cover_rel else None
    if not cover or not cover.exists():
        raise HTTPException(404, "pas de pochette")
    data = cover_pdf(cover)
    return _Response(data, media_type="application/pdf",
                     headers={"Content-Disposition": f'attachment; filename="{slug}-cover_print.pdf"'})


@app.get("/download/{slug}/traycard/pdf")
def download_traycard_printable(slug: str,
                                identity: dict = Depends(require_user)) -> _Response:
    tc, ext = _album_traycard(slug)
    data = traycard_pdf(tc)
    return _Response(data, media_type="application/pdf",
                     headers={"Content-Disposition": f'attachment; filename="{slug}-traycard_print.pdf"'})


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
        "published": m.data.get("published", True),
        "tracks": [{"n": t.get("n"), "title": t.get("title")} for t in m.tracks],
    }


@app.put("/api/albums/{slug}/meta")
def update_album_meta(slug: str, payload: AlbumMetaIn,
                      identity: dict = Depends(require_gestionnaire)) -> dict:
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(path)
    alb = m.data.setdefault("album", {})
    alb["artist"] = payload.artist
    alb["title"] = payload.title
    alb["date"] = payload.date or ""
    alb["venue"] = payload.venue or ""
    alb["festival"] = payload.festival or ""
    src = m.data.setdefault("source", {})
    src["url"] = payload.source_url
    src["label"] = payload.source_label
    m.save()
    tagged = _write_album_tags(slug, m)
    return {"ok": True, "mp3_tagged": tagged}


@app.put("/api/albums/{slug}/tracks")
def update_tracks(slug: str, payload: TracksEditIn,
                  identity: dict = Depends(require_gestionnaire)) -> dict:
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(path)
    track_map = {t["n"]: t for t in m.tracks}
    new_tracks = []
    for ti in payload.tracks:
        existing = dict(track_map.get(ti.n, {"n": ti.n, "start": None, "end": None, "locked": False}))
        existing["n"] = ti.n
        existing["title"] = ti.title
        new_tracks.append(existing)
    m.data["tracks"] = new_tracks
    m.save()

    # Écriture des tags ID3 dans les fichiers MP3
    audio_dir = PROJECTS_DIR / slug / "build" / "audio"
    tagged = 0
    if audio_dir.exists():
        total = len(new_tracks)
        # n original → (nouvelle position, titre)
        pos_by_n = {t["n"]: (i + 1, t["title"]) for i, t in enumerate(new_tracks)}
        for mp3_path in sorted(audio_dir.glob("*.mp3")):
            file_n = _file_track_n(mp3_path.stem)
            if file_n is None or file_n not in pos_by_n:
                continue
            new_pos, title = pos_by_n[file_n]
            try:
                try:
                    tags = EasyID3(str(mp3_path))
                except ID3NoHeaderError:
                    tags = EasyID3()
                    tags.save(str(mp3_path))
                    tags = EasyID3(str(mp3_path))
                tags["tracknumber"] = [f"{new_pos}/{total}"]
                tags["title"] = [title]
                tags.save()
                tagged += 1
            except Exception:
                pass
    # aussi rafraîchir TALB/TPE1/TDRC sur tous les fichiers
    tagged += _write_album_tags(slug, m)
    renamed = _rename_audio_files(slug, m)
    return {"ok": True, "tracks": len(new_tracks), "mp3_tagged": tagged, "renamed": renamed}


@app.post("/api/albums/{slug}/cover")
async def upload_cover(slug: str, file: UploadFile = File(...),
                       identity: dict = Depends(require_gestionnaire)) -> dict:
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    ct = file.content_type or ""
    ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    ext = ext_map.get(ct)
    if not ext:
        ext = Path(file.filename or "cover.jpg").suffix or ".jpg"
    art_dir = PROJECTS_DIR / slug / "artwork"
    art_dir.mkdir(exist_ok=True)
    # Retire une éventuelle ancienne pochette d'une autre extension
    for old in art_dir.glob("cover.*"):
        if old.suffix.lower() != ext:
            old.unlink(missing_ok=True)
    cover_path = art_dir / f"cover{ext}"
    cover_path.write_bytes(await file.read())
    m = Manifest.load(path)
    m.data.setdefault("album", {})["cover"] = f"artwork/cover{ext}"
    m.save()
    embedded = _write_album_cover(slug, m)
    return {"ok": True, "cover": f"artwork/cover{ext}", "mp3_embedded": embedded}


@app.post("/api/albums/{slug}/traycard")
async def upload_traycard(slug: str, file: UploadFile = File(...),
                          identity: dict = Depends(require_gestionnaire)) -> dict:
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    ct = file.content_type or ""
    if "pdf" not in ct and not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "format PDF requis")
    art_dir = PROJECTS_DIR / slug / "artwork"
    art_dir.mkdir(exist_ok=True)
    tc_path = art_dir / "tray_card.pdf"
    tc_path.write_bytes(await file.read())
    return {"ok": True, "traycard": "artwork/tray_card.pdf"}


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


# --- Publication ---


@app.patch("/api/albums/{slug}/published")
def set_published(slug: str, payload: PublishIn,
                  identity: dict = Depends(require_gestionnaire)) -> dict:
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(path)
    m.data["published"] = payload.published
    m.save(path)
    return {"ok": True, "published": payload.published}


# --- Outil (niveau gestionnaire) ------------------------------------------
@app.get("/album/{slug}", response_class=HTMLResponse)
def album_detail_page(slug: str) -> HTMLResponse:
    page = FRONTEND / "album_detail.html"
    if page.exists():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>live2mp3 — album</h1>")

@app.get("/app", response_class=HTMLResponse)
def tool() -> HTMLResponse:
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
