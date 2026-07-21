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
import shutil
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import re
from . import catalogue, entities, jobs, linktool, llm
from .albumfiles import (
    _extract_embedded_cover,
    _rename_audio_files,
    _sanitize_filename,
    _write_album_cover,
    _write_album_tags,
    _write_track_tags,
)
from .auth import (
    GROUP_GESTIONNAIRE,
    SUPERUSER_GROUPS,
    require_gestionnaire,
    require_user,
    roles,
)
from .db import get_conn, init_db
from .import_album import router as import_router
from .manifest import PROJECTS_DIR, Manifest, new_manifest, download_stem
from .pipeline import boundaries, download, preanalyze
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
from .follows import router as follows_router
from . import notifications
from .notifications import router as notifications_router

# Labels dérivés automatiquement de la disponibilité média (non éditables).
DERIVED_LABELS = {"audio", "vidéo", "video", "audio + vidéo", "audio + video"}

BASE = Path(__file__).resolve().parent.parent
FRONTEND = BASE / "frontend"

app = FastAPI(title="live2mp3", docs_url="/api/docs")


@app.middleware("http")
async def _static_revalidate(request, call_next):
    """Force la revalidation des assets statiques (JS/CSS).

    Servis sans `Cache-Control`, les navigateurs les mettaient en cache
    heuristique → un déploiement ne se propageait pas (JS périmé, ex. la refonte
    des réglages de notifications). `no-cache` = le navigateur revalide via
    l'ETag à chaque chargement : 304 si inchangé (quasi gratuit), 200 avec la
    nouvelle version sinon. Plus aucun asset figé après un déploiement.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# Système social (profils, favoris, commentaires) — routes /api/social, /u, /avatar.
app.include_router(social_router)
# Suivi + pages auto d'entités — routes /api/social/follow*, /artist, /festival, /venue.
app.include_router(follows_router)
# Notifications in-app + préférences — routes /api/social/notif*, /notifications, /settings.
app.include_router(notifications_router)
app.include_router(covers_router)

# Import d'un album prêt (dépôt de MP3 ou ZIP) — routes /api/import/*.
app.include_router(import_router)

# Création d'album depuis un lien (analyse yt-dlp + IA) — routes /api/tool/*.
app.include_router(linktool.router)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    # Marque les posts déjà publiés comme « annoncés » (pas de fan-out rétroactif).
    try:
        notifications.ensure_seeded()
    except Exception:
        pass

# --- État de progression en mémoire (par slug) ----------------------------
_progress_bus: dict[str, "queue.Queue[dict]"] = {}
_progress_last: dict[str, list[dict]] = {}

# Détection des chansons passée à la demande : la transcription est le poste le
# plus long et l'humain ajuste les coupes de toute façon. Le drapeau est relu
# par le callback de progression whisper (à chaque segment), donc l'abandon
# prend effet en quelques secondes.
_skip_detection: set[str] = set()


class _SkipDetection(Exception):
    """Abandon volontaire de la détection IA (distinct d'un échec)."""


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
    artist: str | None = None
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
    # Options de l'outil « depuis un lien »
    video: bool = False           # True = télécharger la vidéo + clips MP4
    thumbnail_url: str = ""       # miniature -> proposition de pochette
    duration: float | None = None  # durée de la source (bornage des coupes)
    setlistfm_url: str = ""       # setlist officielle (attribution obligatoire)


class SetlistTrackIn(BaseModel):
    n: int
    title: str
    artist: str | None = None
    start: float
    end: float


class SetlistIn(BaseModel):
    tracks: list[SetlistTrackIn]


class GuestIn(BaseModel):
    id: str
    name: str


class AlbumMetaIn(BaseModel):
    artist: str
    artist_id: str = ""            # id Deezer canonique (liste déroulante)
    title: str
    date: str | None = None
    venue: str | None = None
    city: str | None = None        # ville (optionnelle) — ≠ venue (lieu précis)
    city_id: str = ""              # id canonique OSM (dérivé de la liste)
    festival: str | None = None
    festival_id: str = ""          # slug canonique (dérivé si absent)
    guests: list[GuestIn] = []     # artistes invités canoniques (id Deezer)
    source_url: str = ""
    source_label: str = ""


class TrackEditIn(BaseModel):
    n: int
    title: str


class TracksEditIn(BaseModel):
    tracks: list[TrackEditIn]


class PublishIn(BaseModel):
    published: bool


class PerTrackCoversIn(BaseModel):
    per_track_covers: bool

class TrackMetaIn(BaseModel):
    title: str
    artist: str | None = None


def _list_track_covers_public(slug: str) -> list:
    """Pochettes par piste (publiques) — pour catalogue_detail."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, track_n FROM track_covers WHERE slug=? ORDER BY track_n", (slug,)
        ).fetchall()
    return [{"id": r["id"], "track_n": r["track_n"], "cover_url": f"/track-cover/{r['id']}"} for r in rows]


# --- Santé & version (public) ---------------------------------------------
import os
import httpx

APP_ENV = os.environ.get("APP_ENV", "prod")
GIT_COMMIT = os.environ.get("GIT_COMMIT", "")[:7]
_vfile = BASE / "VERSION"
APP_VERSION = _vfile.read_text().strip() if _vfile.exists() else "0.0.0"

_AUTHENTIK_URL = os.environ.get("AUTHENTIK_URL", "https://auth.naerod.com")
_AUTHENTIK_TOKEN = os.environ.get("AUTHENTIK_API_TOKEN", "")


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


@app.get("/api/logout")
async def logout(x_authentik_username: str | None = Header(default=None)):
    """Détruit toutes les sessions Authentik du user courant, puis redirige vers /."""
    from fastapi.responses import RedirectResponse
    if x_authentik_username and _AUTHENTIK_TOKEN:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{_AUTHENTIK_URL}/api/v3/core/users/?username={x_authentik_username}",
                headers={"Authorization": f"Bearer {_AUTHENTIK_TOKEN}"},
            )
            if r.status_code == 200:
                users = r.json().get("results", [])
                if users:
                    user_pk = users[0]["pk"]
                    sr = await client.get(
                        f"{_AUTHENTIK_URL}/api/v3/core/authenticated_sessions/"
                        f"?user={user_pk}&page_size=100",
                        headers={"Authorization": f"Bearer {_AUTHENTIK_TOKEN}"},
                    )
                    if sr.status_code == 200:
                        for session in sr.json().get("results", []):
                            await client.delete(
                                f"{_AUTHENTIK_URL}/api/v3/core/authenticated_sessions/"
                                f"{session['uuid']}/",
                                headers={"Authorization": f"Bearer {_AUTHENTIK_TOKEN}"},
                            )
    return RedirectResponse(url="/", status_code=302)


def _norm_title(s: str) -> str:
    """Normalise pour comparer titres et noms de fichiers : minuscules, sans
    ponctuation ni casse, espaces compactés."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _track_file(project_dir: Path, n: int, title: str = "") -> Path | None:
    """Retrouve le MP3 d'une piste.

    Priorité au **titre** : les fichiers le portent quasi toujours, quel que
    soit leur préfixe (`02.`, `[SPOTDOWNLOADER.COM]`, artiste…), et ce préfixe
    numérique ne correspond pas forcément au `n` du manifest (imports externes).
    Le matching par numéro de piste ne sert plus que de secours pour les albums
    rendus par le pipeline dont le fichier n'embarque pas le titre.
    """
    src = project_dir / "build" / "audio"
    if not src.exists():
        return None
    files = sorted(src.glob("*.mp3"))
    nt = _norm_title(title)
    if nt:
        for f in files:
            if nt in _norm_title(f.stem):
                return f
    pat = re.compile(rf"^0*{int(n)}(?=\D)")
    for f in files:
        if pat.match(f.name):
            return f
    return None


def _is_gestionnaire_identity(identity: dict) -> bool:
    """Vrai pour un gestionnaire/admin, quel que soit le Depends d'origine."""
    if identity.get("is_gestionnaire"):
        return True
    groups = identity.get("groups") or set()
    return bool(set(groups) & ({GROUP_GESTIONNAIRE} | SUPERUSER_GROUPS))


def _ensure_album_visible(slug: str, identity: dict) -> Manifest:
    """404 si l'album est dépublié et que le lecteur n'est pas gestionnaire.

    Un album dépublié n'existe pas pour le public : même code de réponse
    qu'un slug inconnu, pour ne pas révéler sa présence.
    """
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(path)
    if not m.data.get("published", True) and not _is_gestionnaire_identity(identity):
        raise HTTPException(404, "album introuvable")
    return m


@app.get("/api/catalogue/{slug}")
def catalogue_detail(slug: str, identity: dict = Depends(roles)) -> dict:
    """Fiche d'un album : métadonnées, labels, setlist (titres)."""
    m = _ensure_album_visible(slug, identity)
    project_dir = PROJECTS_DIR / slug
    cat = {a["slug"]: a for a in
           catalogue.list_albums(include_drafts=_is_gestionnaire_identity(identity))
           }.get(slug, {})
    tracks = []
    for t in m.tracks:
        tracks.append({
            "n": t.get("n"),
            "title": t.get("title"),
            "dl": _track_file(project_dir, t.get("n"), t.get("title", "")) is not None,
        })
    meta = m.data.get("meta", {})
    src = m.data.get("source", {})
    # Pochette « automatique » = miniature récupérée par l'import auto (outil de
    # lien). Distinguée des pochettes faites main : celles-ci sont soit des
    # `legacy_cover` (import historique), soit un téléversement `artwork/cover.ext`,
    # et leur album n'a pas `import_source == "url"`. On exige donc l'import auto
    # ET une pochette issue du dossier covers/ qui ne soit pas la legacy.
    _cover_rel = str(m.data.get("album", {}).get("cover", "") or "")
    _cover_name = _cover_rel.rsplit("/", 1)[-1]
    cover_auto = (
        meta.get("import_source") == "url"
        and _cover_rel.startswith("artwork/covers/")
        and not _cover_name.startswith("legacy_cover")
    )
    return {
        "slug": slug,
        "album": m.data.get("album", {}),
        # Entités canoniques liées (artiste, festival, lieu) : ids exacts pour
        # rendre les noms cliquables vers leurs pages auto.
        "entities": entities.album_entities(m.data.get("album", {})),
        "labels": cat.get("labels", []),
        "has_cover": cat.get("has_cover", False),
        "has_traycard": cat.get("has_traycard", False),
        "has_mp3": cat.get("has_mp3", False),
        "has_mp4": cat.get("has_mp4", False),
        "tracks": tracks,
        "imported_by": meta.get("imported_by", ""),
        "imported_at": meta.get("imported_at", ""),
        # Provenance affichée sur la fiche : "url" = import auto par l'outil de
        # lien, sinon album créé/renseigné manuellement.
        "import_source": meta.get("import_source", "") or "",
        "cover_auto": cover_auto,
        "source_url": src.get("url", "") or "",
        # Attribution setlist.fm — obligatoire partout où la donnée est affichée.
        "setlistfm_url": meta.get("setlistfm_url", "") or "",
        "source_label": src.get("label", "") or "",
        "per_track_covers": bool(m.data.get("album", {}).get("per_track_covers", False)),
        "track_covers": _list_track_covers_public(slug),
        "published": m.data.get("published", True),
    }


@app.get("/api/catalogue/{slug}/nav")
def catalogue_nav(slug: str, identity: dict = Depends(roles)) -> dict:
    """Album précédent / suivant dans l'ordre date_import (desc)."""
    include_drafts = identity.get("is_gestionnaire", False)
    albums = catalogue.list_albums(sort="date_import", include_drafts=include_drafts)
    slugs = [a["slug"] for a in albums]
    if slug not in slugs:
        raise HTTPException(404, "album introuvable")
    idx = slugs.index(slug)
    return {
        "prev": slugs[idx - 1] if idx > 0 else None,
        "next": slugs[idx + 1] if idx < len(slugs) - 1 else None,
    }



@app.get("/cover/{slug}")
def get_cover(slug: str, identity: dict = Depends(roles)) -> FileResponse:
    m = _ensure_album_visible(slug, identity)
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
    m = _ensure_album_visible(slug, identity)
    project_dir = PROJECTS_DIR / slug
    title = next((t.get("title", "") for t in m.tracks if t.get("n") == n), "")
    f = _track_file(project_dir, n, title)
    if not f:
        raise HTTPException(404, "piste introuvable")
    # Nom de téléchargement propre (les fichiers source ont des noms parasites :
    # « [SPOTDOWNLOADER.COM]… », préfixes numériques incohérents…).
    dl_name = f"{title}.mp3" if title else f.name
    return FileResponse(f, filename=dl_name, media_type="audio/mpeg")


@app.get("/download/{slug}/cover")
def download_cover(slug: str, identity: dict = Depends(require_user)) -> FileResponse:
    m = _ensure_album_visible(slug, identity)
    cover_rel = m.data.get("album", {}).get("cover")
    cover = PROJECTS_DIR / slug / cover_rel if cover_rel else None
    if not cover or not cover.exists():
        raise HTTPException(404, "pas de pochette")
    return FileResponse(cover, filename=f"{download_stem(m.data, slug)}_cover{cover.suffix}")


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
    m = _ensure_album_visible(slug, identity)
    tc, ext = _album_traycard(slug)
    return FileResponse(tc, filename=f"{download_stem(m.data, slug)}_traycard{ext}",
                        media_type=MEDIA_TYPES.get(ext, "application/pdf"))


@app.get("/app/traycard/{slug}")
def view_traycard(slug: str, identity: dict = Depends(require_gestionnaire)) -> FileResponse:
    """Même fichier, servi inline pour la prévisualisation en iframe."""
    tc, ext = _album_traycard(slug)
    return FileResponse(tc, media_type=MEDIA_TYPES.get(ext, "application/pdf"),
                        content_disposition_type="inline")


@app.get("/app/traycard-thumb/{slug}")
def traycard_thumb(slug: str, identity: dict = Depends(require_gestionnaire)):
    """Thumbnail JPEG page 1 de la tray card (prévisualisation sans iframe)."""
    from fastapi.responses import Response as _Resp
    from PIL import Image as _Image
    import pypdfium2 as _pdfium

    tc, ext = _album_traycard(slug)
    thumb = tc.with_suffix(".thumb.jpg")

    if not thumb.exists() or thumb.stat().st_mtime < tc.stat().st_mtime:
        if ext == ".pdf":
            pdf = _pdfium.PdfDocument(str(tc))
            page = pdf[0]
            bm = page.render(scale=1.5)
            img = bm.to_pil()
            pdf.close()
        else:
            img = _Image.open(tc)

        if img.mode != "RGB":
            bg = _Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)
            img = bg
        img.save(str(thumb), "JPEG", quality=85, optimize=True)

    return FileResponse(str(thumb), media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache, must-revalidate"})


from fastapi.responses import Response as _Response


@app.get("/download/{slug}/cover/pdf")
def download_cover_printable(slug: str,
                             identity: dict = Depends(require_user)) -> _Response:
    m = _ensure_album_visible(slug, identity)
    cover_rel = m.data.get("album", {}).get("cover")
    cover = PROJECTS_DIR / slug / cover_rel if cover_rel else None
    if not cover or not cover.exists():
        raise HTTPException(404, "pas de pochette")
    data = cover_pdf(cover)
    return _Response(data, media_type="application/pdf",
                     headers={"Content-Disposition": f'attachment; filename="{download_stem(m.data, slug)}_cover_print.pdf"'})


@app.get("/download/{slug}/traycard/pdf")
def download_traycard_printable(slug: str,
                                identity: dict = Depends(require_user)) -> _Response:
    m = _ensure_album_visible(slug, identity)
    tc, ext = _album_traycard(slug)
    data = traycard_pdf(tc)
    return _Response(data, media_type="application/pdf",
                     headers={"Content-Disposition": f'attachment; filename="{download_stem(m.data, slug)}_traycard_print.pdf"'})


@app.get("/download/{slug}/{kind}")
def download_media(slug: str, kind: str,
                   identity: dict = Depends(require_user)) -> FileResponse:
    if kind not in ("mp3", "mp4"):
        raise HTTPException(400, "type invalide (mp3|mp4)")
    m = _ensure_album_visible(slug, identity)
    project_dir = PROJECTS_DIR / slug
    zip_path = _zip_media(project_dir, kind)
    stem = download_stem(m.data, slug)
    return FileResponse(zip_path, filename=f"{stem}_{kind}.zip",
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
    src = m.data.get("source", {}) or {}
    cat = {a["slug"]: a for a in catalogue.list_albums()}.get(slug, {})
    return {
        "slug": slug,
        "album": album,
        # Source affichée/éditée sur la fiche. Stockée sous `source` (pas sous
        # `album`) : l'import y met aussi master_mkv, thumbnail… on n'expose que
        # les deux champs saisissables, sinon le formulaire les perd au save.
        "source_url": src.get("url", "") or "",
        "source_label": src.get("label", "") or "",
        "labels": list(album.get("labels", []) or []),
        "derived_labels": [l for l in cat.get("labels", []) if l in DERIVED_LABELS],
        "all_labels": catalogue.all_labels(),
        "has_cover": cat.get("has_cover", False),
        "has_traycard": cat.get("has_traycard", False),
        "has_mp3": cat.get("has_mp3", False),
        "has_mp4": cat.get("has_mp4", False),
        "published": m.data.get("published", True),
        "per_track_covers": bool(album.get("per_track_covers", False)),
        "tracks": [{"n": t.get("n"), "title": t.get("title"), **({} if not t.get("artist") else {"artist": t.get("artist")})} for t in m.tracks],
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
    # Ville (optionnelle). Nettoyée si vide pour garder le manifest lisible.
    if payload.city and payload.city.strip():
        alb["city"] = payload.city.strip()
        if payload.city_id.strip():
            alb["city_id"] = payload.city_id.strip()
        else:
            alb.pop("city_id", None)
    else:
        alb.pop("city", None)
        alb.pop("city_id", None)

    # Champs canoniques (liens de suivi / pages auto). Nettoyés s'ils sont vides
    # pour garder le manifest lisible.
    if payload.artist_id.strip():
        alb["artist_id"] = payload.artist_id.strip()
    else:
        alb.pop("artist_id", None)
    if payload.festival:
        alb["festival_id"] = payload.festival_id.strip() or entities.festival_slug(payload.festival)
    else:
        alb.pop("festival_id", None)
    guests = [{"id": g.id.strip(), "name": g.name.strip()}
              for g in payload.guests if g.id.strip() and g.name.strip()]
    if guests:
        alb["guests"] = guests
    else:
        alb.pop("guests", None)

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

    # Tags ID3 par piste (titre + numéro), mapping fichier↔piste par titre
    # (robuste aux noms de fichiers hétérogènes), puis tags communs à l'album.
    tagged = _write_track_tags(slug, m)
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
    tc_path.with_suffix(".thumb.jpg").unlink(missing_ok=True)
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
    # Publication → annonce aux abonnés (fan-out idempotent : une fois par env).
    notified = 0
    if payload.published:
        try:
            notified = notifications.announce_post(slug)
        except Exception:
            notified = 0
    return {"ok": True, "published": payload.published, "notified": notified}


# --- Outil (niveau gestionnaire) ------------------------------------------


@app.delete("/api/albums/{slug}/cover/auto")
def delete_album_cover_auto(
    slug: str, identity: dict = Depends(require_gestionnaire)
) -> dict:
    """Supprime la pochette auto-extraite du dossier artwork/ (ne touche pas aux covers piste)."""
    artwork_dir = PROJECTS_DIR / slug / "artwork"
    deleted = False
    for ext in [".jpg", ".jpeg", ".png", ".webp"]:
        f = artwork_dir / f"cover{ext}"
        if f.exists():
            f.unlink()
            deleted = True
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if path.exists():
        m = Manifest.load(path)
        m.data.setdefault("album", {}).pop("cover", None)
        m.save()
    return {"ok": True, "deleted": deleted}

@app.patch("/api/albums/{slug}/per-track-covers")
def set_per_track_covers(slug: str, payload: PerTrackCoversIn,
                          identity: dict = Depends(require_gestionnaire)) -> dict:
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(path)
    m.data.setdefault("album", {})["per_track_covers"] = payload.per_track_covers
    m.save()
    return {"ok": True, "per_track_covers": payload.per_track_covers}

@app.patch("/api/albums/{slug}/tracks/{n}/meta")
def patch_track_meta(
    slug: str, n: int, payload: TrackMetaIn,
    identity: dict = Depends(require_gestionnaire),
) -> dict:
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(path)
    track = next((t for t in m.data.get("tracks", []) if t["n"] == n), None)
    if track is None:
        raise HTTPException(404, "piste introuvable")
    track["title"] = payload.title.strip()
    if payload.artist is not None:
        if payload.artist.strip():
            track["artist"] = payload.artist.strip()
        else:
            track.pop("artist", None)
    m.save()
    return {"ok": True, "track": track}

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
    tracks = [t.model_dump(exclude_none=True) for t in job.tracks]
    # Setlist inconnue : piste unique provisoire, l'IA la remplacera à la
    # préparation (transcription -> identification des chansons).
    auto_setlist = not tracks
    if auto_setlist:
        tracks = [{"n": 1, "title": job.album.title or "Piste 1"}]
    m = new_manifest(
        job.album.model_dump(exclude_none=True),
        tracks,
        target=job.target, source_url=job.source_url,
    )
    project_dir = PROJECTS_DIR / m.slug
    if (project_dir / "manifest.yaml").exists():
        raise HTTPException(
            409, f"un album existe déjà sous ce slug ({m.slug}) — "
                 "modifier l'artiste ou la date")
    m.data["auto_setlist"] = auto_setlist
    m.data["source"]["media"] = "video" if job.video else "audio"
    if job.thumbnail_url:
        m.data["source"]["thumbnail_url"] = job.thumbnail_url
    if job.duration:
        m.data["source"]["duration"] = float(job.duration)
    m.data["meta"] = {
        "imported_by": identity.get("username") or "",
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "import_source": "url",
    }
    if job.setlistfm_url:
        # L'attribution suit la donnée : affichée sur la fiche album (ToS).
        m.data["meta"]["setlistfm_url"] = job.setlistfm_url
    # Un album créé par l'outil naît dépublié : le volume est partagé
    # prod/preprod, rien ne doit devenir public avant validation explicite.
    m.data["published"] = False
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


def _fallback_markers(n: int, duration: float,
                      candidates: list[dict]) -> dict[int, dict[str, float]]:
    """Découpe sans LLM : les n-1 creux d'énergie les plus marqués.

    Sur un concert réel cette seule mesure place déjà les coupes correctement ;
    la répartition uniforme n'est qu'un dernier recours (audio trop uniforme).
    """
    if n <= 0 or duration <= 0:
        return {}
    cuts = [t for t in boundaries.pick(candidates, n - 1) if 0 < t < duration]
    if len(cuts) < n - 1:
        cuts = [duration * i / n for i in range(1, n)]
    bounds = [0.0] + list(cuts) + [duration]
    return {i + 1: {"start": bounds[i], "end": bounds[i + 1]} for i in range(n)}


def _snap_markers(markers: dict[int, dict[str, float]],
                  candidates: list[dict]) -> dict[int, dict[str, float]]:
    """Recale les frontières proposées par l'IA sur le creux d'énergie voisin.

    L'IA situe correctement une transition à quelques secondes près (elle lit
    les paroles) mais pas à la seconde : le creux mesuré, lui, est exact. Une
    frontière sans creux à portée est laissée telle quelle (enchaînement sans
    coupure, medley).
    """
    if not candidates or not markers:
        return markers
    for n in sorted(markers):
        mk = markers[n]
        # Le début de la 1re piste et la fin de la dernière ne sont pas des
        # transitions : on ne les déplace pas.
        if n > min(markers):
            mk["start"] = boundaries.snap([mk["start"]], candidates)[0]
        if n < max(markers):
            mk["end"] = boundaries.snap([mk["end"]], candidates)[0]
    # Recolle les pistes entre elles : une frontière est commune.
    ordered = sorted(markers)
    for a, b in zip(ordered, ordered[1:]):
        markers[b]["start"] = markers[a]["end"]
    return markers


def _snap_tracks(tracks: list[dict], candidates: list[dict],
                 duration: float) -> list[dict]:
    """Idem pour une setlist produite de bout en bout par l'IA."""
    if not candidates or not tracks:
        return tracks
    for i, t in enumerate(tracks):
        if i > 0:
            t["start"] = boundaries.snap([t["start"]], candidates)[0]
        if i < len(tracks) - 1:
            t["end"] = boundaries.snap([t["end"]], candidates)[0]
    for a, b in zip(tracks, tracks[1:]):
        b["start"] = a["end"]
    if duration:
        tracks[-1]["end"] = min(tracks[-1]["end"], duration)
    return tracks


def _run_prepare_bg(slug: str, username: str) -> None:
    """Téléchargement -> preview -> waveform -> pochette -> marqueurs IA."""
    project_dir = PROJECTS_DIR / slug
    cb = _make_cb(slug)
    try:
        m = Manifest.load(project_dir / "manifest.yaml")
        src = m.data.get("source", {})

        if m.state("download") != "done":
            cb("download", "running", {})
            def dl_pct(pct: float) -> None:
                _publish(slug, {"stage": "download", "status": "running",
                                "info": {"pct": pct}, "ts": time.time()})
            download.run(project_dir, progress=dl_pct)
            # download.run a sauvé son état sur sa propre instance : recharger
            # avant toute écriture, sinon on ré-écrirait download=pending.
            m = Manifest.load(project_dir / "manifest.yaml")
        cb("download", "done", {})

        cb("preview", "running", {})
        linktool.make_preview(project_dir, m)
        cb("preview", "done", {})

        wav = project_dir / src["master_wav"]
        cb("waveform", "running", {})
        preanalyze.generate_waveform(wav, project_dir / "source" / "waveform.dat")
        m.set_state("waveform", "done")
        cb("waveform", "done", {})

        thumb = src.get("thumbnail_url")
        if thumb:
            try:
                linktool.register_thumbnail_cover(slug, thumb, username)
                m = Manifest.load(project_dir / "manifest.yaml")  # album.cover mis à jour
            except Exception:
                pass  # pochette proposable à la main plus tard, non bloquant

        duration = float(src.get("duration") or 0) or _wav_duration(wav)
        missing = [t for t in m.tracks
                   if t.get("start") is None or t.get("end") is None]
        if not missing and not m.data.get("auto_setlist"):
            m.set_state("ai_markers", "done")
            cb("ai_markers", "done", {"source": "timecodes"})
        else:
            # `skippable` signale au front qu'il peut proposer « passer ».
            cb("ai_markers", "running", {"phase": "silences", "skippable": True})
            # Creux d'énergie = vraies transitions d'un live (les silences
            # absolus n'existent pas : applaudissements, foule, annonces).
            cands = boundaries.detect(wav)
            source = "ia"
            try:
                if slug in _skip_detection:
                    raise _SkipDetection()
                # Phase transcription : d'abord sans pct (chargement du modèle
                # -> barre indéterminée), puis pct au fil des segments whisper.
                _publish(slug, {"stage": "ai_markers", "status": "running",
                                "info": {"phase": "transcription",
                                         "skippable": True}, "ts": time.time()})
                _tr_last = [0.0]
                def tr_pct(frac: float) -> None:
                    # Point d'abandon : appelé à chaque segment transcrit.
                    if slug in _skip_detection:
                        raise _SkipDetection()
                    pct = round(frac * 100, 1)
                    if pct - _tr_last[0] >= 1 or pct >= 100:
                        _tr_last[0] = pct
                        _publish(slug, {"stage": "ai_markers", "status": "running",
                                        "info": {"phase": "transcription", "pct": pct,
                                                 "skippable": True},
                                        "ts": time.time()})
                transcript = preanalyze.transcribe(wav, progress=tr_pct)
                if slug in _skip_detection:
                    raise _SkipDetection()
                _publish(slug, {"stage": "ai_markers", "status": "running",
                                "info": {"phase": "llm"}, "ts": time.time()})
                if m.data.get("auto_setlist"):
                    tracks = llm.request_auto_setlist(
                        transcript, cands,
                        m.data["album"].get("artist", ""), duration)
                    if not tracks:
                        raise RuntimeError("setlist auto vide")
                    for t in tracks:
                        t["locked"] = False
                        if not t.get("artist"):
                            t.pop("artist", None)
                    m.data["tracks"] = _snap_tracks(tracks, cands, duration)
                    m.save()
                else:
                    setlist = [{"n": t["n"], "title": t["title"]}
                               for t in m.tracks]
                    markers = llm.request_markers(setlist, transcript, cands)
                    m.merge_ai_markers(_snap_markers(markers, cands))
            except _SkipDetection:
                # Passage volontaire : mêmes frontières de secours (sur les
                # creux d'énergie), l'humain les place ensuite dans l'éditeur.
                markers = _fallback_markers(len(m.tracks), duration, cands)
                m.merge_ai_markers(markers)
                source = "skipped"
            except Exception:
                # IA indisponible : découpe de secours pour que l'éditeur
                # s'ouvre quand même — l'humain replace les frontières.
                markers = _fallback_markers(len(m.tracks), duration, cands)
                m.merge_ai_markers(markers)
                source = "fallback"
            finally:
                _skip_detection.discard(slug)
            m.set_state("ai_markers", "done")
            cb("ai_markers", "done", {"source": source})

        _publish(slug, {"stage": "prepare", "status": "complete", "info": {},
                        "ts": time.time()})
    except Exception as e:  # pragma: no cover
        _publish(slug, {"stage": "error", "status": "error",
                        "info": {"message": str(e)}, "ts": time.time()})


def _wav_duration(wav: Path) -> float:
    try:
        import wave
        with wave.open(str(wav), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


@app.post("/api/jobs/{slug}/prepare")
def start_prepare(slug: str,
                  identity: dict = Depends(require_gestionnaire)) -> dict:
    project_dir = PROJECTS_DIR / slug
    if not (project_dir / "manifest.yaml").exists():
        raise HTTPException(404, "projet introuvable")
    _progress_last[slug] = []
    _progress_bus[slug] = queue.Queue()
    _skip_detection.discard(slug)   # repart d'un état propre
    threading.Thread(target=_run_prepare_bg,
                     args=(slug, identity.get("username") or ""),
                     daemon=True).start()
    return {"ok": True, "slug": slug}


@app.post("/api/jobs/{slug}/skip-detection")
def skip_detection(slug: str,
                   identity: dict = Depends(require_gestionnaire)) -> dict:
    """Abandonne la détection IA en cours : coupes de secours puis éditeur.

    La transcription est le poste le plus long du pipeline et l'humain ajuste
    les coupes de toute façon — ce raccourci mène directement à l'éditeur.
    """
    if not (PROJECTS_DIR / slug / "manifest.yaml").exists():
        raise HTTPException(404, "projet introuvable")
    _skip_detection.add(slug)
    return {"ok": True, "slug": slug}


@app.get("/api/jobs/{slug}/audio")
def job_audio(slug: str,
              identity: dict = Depends(require_gestionnaire)) -> FileResponse:
    """Audio de travail pour l'éditeur de coupes (preview légère)."""
    preview = PROJECTS_DIR / slug / "source" / "preview.mp3"
    if preview.exists():
        return FileResponse(preview, media_type="audio/mpeg")
    wav = PROJECTS_DIR / slug / "source" / "master.wav"
    if wav.exists():
        return FileResponse(wav, media_type="audio/wav")
    raise HTTPException(404, "audio absent (préparation non faite)")


@app.put("/api/jobs/{slug}/setlist")
def update_setlist(slug: str, payload: SetlistIn,
                   identity: dict = Depends(require_gestionnaire)) -> dict:
    """Setlist validée dans l'éditeur : remplace les pistes et verrouille."""
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "projet introuvable")
    if not payload.tracks:
        raise HTTPException(400, "au moins une piste requise")
    tracks = sorted(payload.tracks, key=lambda t: t.start)
    for t in tracks:
        if not t.title.strip():
            raise HTTPException(400, "chaque piste doit avoir un titre")
        if t.end <= t.start:
            raise HTTPException(400, f"piste « {t.title} » : fin avant début")
    m = Manifest.load(path)
    m.data["tracks"] = [{
        "n": i,
        "title": t.title.strip(),
        **({"artist": t.artist.strip()} if t.artist and t.artist.strip() else {}),
        "start": float(t.start),
        "end": float(t.end),
        "locked": True,
    } for i, t in enumerate(tracks, start=1)]
    m.data["auto_setlist"] = False
    m.save()
    return {"ok": True, "tracks": len(m.tracks)}


@app.delete("/api/jobs/{slug}")
def delete_job(slug: str,
               identity: dict = Depends(require_gestionnaire)) -> dict:
    """Supprime un brouillon (projet jamais rendu). Refus si l'album existe."""
    project_dir = PROJECTS_DIR / slug
    if not (project_dir / "manifest.yaml").exists():
        raise HTTPException(404, "projet introuvable")
    if any((project_dir / "build" / "audio").glob("*.mp3")):
        raise HTTPException(409, "album déjà rendu — suppression refusée")
    linktool.delete_project_social(slug)
    shutil.rmtree(project_dir, ignore_errors=True)
    _progress_last.pop(slug, None)
    _progress_bus.pop(slug, None)
    return {"ok": True}


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


def _run_render_bg(slug: str, media: str, gap: float, video: bool) -> None:
    project_dir = PROJECTS_DIR / slug
    try:
        jobs.run_render_pipeline(project_dir, media=media, gap_seconds=gap,
                                 video=video, progress=_make_cb(slug))
        _publish(slug, {"stage": "all", "status": "complete", "info": {},
                        "ts": time.time()})
    except Exception as e:  # pragma: no cover
        _publish(slug, {"stage": "error", "status": "error",
                        "info": {"message": str(e)}, "ts": time.time()})


@app.post("/api/jobs/{slug}/render")
def start_render(slug: str, media: str = "audio", gap: float = 2.0,
                 video: bool | None = None,
                 identity: dict = Depends(require_gestionnaire)) -> dict:
    project_dir = PROJECTS_DIR / slug
    if not (project_dir / "manifest.yaml").exists():
        raise HTTPException(404, "projet introuvable")
    if video is None:
        # Clips MP4 seulement si la source vidéo a été téléchargée.
        m = Manifest.load(project_dir / "manifest.yaml")
        video = m.data.get("source", {}).get("media", "video") == "video"
    _progress_last[slug] = []
    _progress_bus[slug] = queue.Queue()
    threading.Thread(target=_run_render_bg, args=(slug, media, gap, video),
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
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/jobs/{slug}/bundle")
def download_bundle(slug: str,
                    identity: dict = Depends(require_gestionnaire)) -> FileResponse:
    path = PROJECTS_DIR / slug / "build" / "bundle.zip"
    if not path.exists():
        raise HTTPException(404, "bundle non généré")
    manifest_path = PROJECTS_DIR / slug / "manifest.yaml"
    stem = download_stem(Manifest.load(manifest_path).data, slug) \
        if manifest_path.exists() else slug
    return FileResponse(path, filename=f"{stem}.zip",
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
