"""API FastAPI — formulaire, création de jobs, progression SSE, fichiers.

Accès prévu Tailscale only (allowlist Nginx en amont). Les jobs longs sont
délégués à RQ/Redis : le rendu tourne dans le worker, pas ici. La progression
est publiée par projet dans Redis et relue via SSE.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import os
import tempfile

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from starlette.background import BackgroundTask
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import re
from uuid import uuid4
from . import catalogue, entities, jellyfin, linktool, llm
from . import progress, renderqueue, setlistfm
from .albumfiles import (
    _rename_audio_files,
    _write_album_cover,
    _write_album_tags,
    _write_track_tags,
)
from .auth import (
    GROUP_APP_ADMIN,
    GROUP_GESTIONNAIRE,
    SUPERUSER_GROUPS,
    require_gestionnaire,
    require_user,
    roles,
)
from .db import get_conn, init_db
from . import addtrack
from .addtrack import router as addtrack_router
from .recut import router as recut_router
from .import_album import router as import_router
from .manifest import PROJECTS_DIR, Manifest, new_manifest, download_stem
from . import slugrename
from .pipeline import boundaries, bundle, download, preanalyze
from .covers import (
    COVER_EXTS,
    COVER_MAX_BYTES,
    MEDIA_TYPES,
    _on_covers_changed,
    _read_upload as _covers_read_upload,
    cover_file,
    covers_dir,
    rank_covers,
    router as covers_router,
    top_cover,
    traycard_file,
    zip_basename,
)
from .printable import cover_pdf, traycard_pdf
from .social import (
    router as social_router,
    _album_exists,
    _ensure_profile,
    _profiles_map,
)
from .follows import router as follows_router
from . import notifications
from .notifications import router as notifications_router
import logging

log = logging.getLogger(__name__)

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
app.include_router(addtrack_router)
# Re-couper une piste à la waveform avec ses 2 voisines + cadenas de liaison.
app.include_router(recut_router)

# Création d'album depuis un lien (analyse yt-dlp + IA) — routes /api/tool/*.
app.include_router(linktool.router)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    # Marque les posts déjà publiés comme « annoncés » (pas de fan-out rétroactif).
    try:
        notifications.ensure_seeded()
    except Exception as exc:
        log.warning("amorçage des notifications impossible : %s", exc)

# --- État de progression (Redis, partagé API <-> worker) ------------------
# Le rendu s'exécute dans le worker RQ : l'émetteur et le lecteur du flux SSE
# ne sont plus dans le même process, un dictionnaire en mémoire ne suffit plus.
# Voir backend/progress.py.

# Détection des chansons passée à la demande : la transcription est le poste le
# plus long et l'humain ajuste les coupes de toute façon. Le drapeau est relu
# par le callback de progression whisper (à chaque segment), donc l'abandon
# prend effet en quelques secondes.
_skip_detection: set[str] = set()


class _SkipDetection(Exception):
    """Abandon volontaire de la détection IA (distinct d'un échec)."""


def _publish(slug: str, event: dict) -> None:
    progress.publish(slug, event)


def _make_cb(slug: str):
    return progress.make_cb(slug)


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
    # Id Deezer canonique choisi dans la liste déroulante : c'est lui qui relie
    # l'album à sa page artiste, indépendamment de l'orthographe saisie.
    artist_id: str | None = None
    title: str
    date: str | None = None
    venue: str | None = None
    festival: str | None = None
    cover: str | None = None


class ClipIn(BaseModel):
    url: str
    title: str
    artist: str | None = None
    duration: float | None = None


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
    # Mode multi-liens : chaque clip devient une piste, sources concaténées
    # en un master unique à la préparation. Prime sur source_url/tracks.
    clips: list[ClipIn] = []


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
    tour: str | None = None        # tournée (ex. « The Clancy World Tour »)
    subtitle: str | None = None    # texte bonus optionnel (nom d'album live, etc.)
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


class BulkPublishIn(BaseModel):
    slugs: list[str]
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
    return RedirectResponse(url="/outpost.goauthentik.io/sign_out?rd=/", status_code=302)


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
    # lien), marquée `auto=1` en base. Le crédit se lit donc sur la pochette
    # RÉELLEMENT gagnante : dès qu'une pochette manuelle passe devant (elles ont
    # la priorité au classement), le badge « automatique » disparaît de lui-même.
    with get_conn() as conn:
        _top = top_cover(conn, slug)
    cover_auto = bool(_top and _top["auto"])
    return {
        "slug": slug,
        "album": m.data.get("album", {}),
        # Entités canoniques liées (artiste, festival, lieu) : ids exacts pour
        # rendre les noms cliquables vers leurs pages auto.
        "entities": entities.album_entities(m.data.get("album", {})),
        "labels": cat.get("labels", []),
        "has_cover": cat.get("has_cover", False),
        "cover_v": cat.get("cover_v", 0),
        "has_traycard": cat.get("has_traycard", False),
        "has_mp3": cat.get("has_mp3", False),
        "has_mp4": cat.get("has_mp4", False),
        "has_video_full": cat.get("has_video_full", False),
        "status": cat.get("status") or catalogue.album_status(
            m.data.get("published", True), cat.get("has_mp3", False),
            cat.get("has_mp4", False), cat.get("has_video_full", False)),
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
    # Le front ajoute ?v=<mtime> pour invalider dès qu'un gestionnaire remplace
    # la cover. On force la revalidation pour rattraper les vieux liens sans v=.
    return FileResponse(cover, headers={
        "Cache-Control": "no-cache, must-revalidate",
    })


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
    # Nom de téléchargement propre et numéroté (« 11. Heavy Dirty Soul.mp3 »)
    # pour l'ordre ; les fichiers source ont des noms parasites.
    from .manifest import numbered_title, sanitize_filename
    dl_name = f"{sanitize_filename(numbered_title(n, title))}.mp3" if title else f.name
    return FileResponse(f, filename=dl_name, media_type="audio/mpeg")


@app.get("/download/{slug}/video")
def download_video(slug: str, identity: dict = Depends(require_user)) -> FileResponse:
    """Télécharge le MP4 concert-complet (build/video-full). Streaming natif
    (FileResponse) — pas de mise en RAM, adapté aux fichiers de plusieurs Go."""
    m = _ensure_album_visible(slug, identity)
    vfdir = PROJECTS_DIR / slug / "build" / "video-full"
    mp4s = sorted(vfdir.glob("*.mp4")) if vfdir.exists() else []
    if not mp4s:
        raise HTTPException(404, "pas de concert complet")
    return FileResponse(mp4s[0], filename=mp4s[0].name, media_type="video/mp4")


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


@app.get("/download/bulk")
def download_bulk(kind: str = "mp3",
                  slugs: list[str] = Query(default=[]),
                  identity: dict = Depends(require_user)) -> FileResponse:
    """Un seul ZIP regroupant plusieurs albums, un sous-dossier par album.

    En **GET** (et non POST) pour que le front déclenche un téléchargement natif
    du navigateur (barre de progression, pas de blob 100 % en mémoire, pas de
    bufferisation Cloudflare d'une réponse fetch). Le zip est écrit sur disque
    puis servi avec un Content-Length, puis supprimé après envoi.

    On réutilise le zip par album (déjà mis en cache par `_zip_media`) et on le
    stocke tel quel (ZIP_STORED : pas de recompression de médias déjà zippés).
    Les albums invisibles pour le lecteur (dépubliés, non gestionnaire) ou sans
    média du type demandé sont ignorés silencieusement — on ne révèle rien.
    """
    if kind not in ("mp3", "mp4"):
        raise HTTPException(400, "type invalide (mp3|mp4)")
    # Dédoublonnage en gardant l'ordre de sélection.
    slugs = list(dict.fromkeys(slugs))
    if not slugs:
        raise HTTPException(400, "aucun album sélectionné")

    entries: list[tuple[Path, str]] = []
    for slug in slugs:
        try:
            m = _ensure_album_visible(slug, identity)
            zip_path = _zip_media(PROJECTS_DIR / slug, kind)
        except HTTPException:
            continue  # invisible ou sans média du type → on saute
        stem = download_stem(m.data, slug)
        entries.append((zip_path, f"{stem}/{stem}_{kind}.zip"))

    if not entries:
        raise HTTPException(404, f"aucun album téléchargeable ({kind})")

    fd, tmp = tempfile.mkstemp(prefix="l2m_bulk_", suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for src, arc in entries:
            z.write(src, arc)

    name = f"live2mp3_selection_{kind}_{len(entries)}.zip"
    return FileResponse(tmp, media_type="application/zip", filename=name,
                        background=BackgroundTask(os.remove, tmp))


# --- Gestion d'album (niveau gestionnaire) --------------------------------
@app.get("/api/albums/{slug}")
def album_detail(slug: str,
                 identity: dict = Depends(require_gestionnaire)) -> dict:
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(path)
    if catalogue.hidden_by_env(m.data):
        raise HTTPException(404, "album introuvable")
    album = m.data.get("album", {})
    src = m.data.get("source", {}) or {}
    cat = {a["slug"]: a for a in catalogue.list_albums()}.get(slug, {})
    src_dir = PROJECTS_DIR / slug / "source"
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
        # L'éditeur de coupes a besoin du master : seuls les albums importés
        # via l'outil lien (pas les imports manuels) le conservent.
        # `env` = environnement qui sert la fiche ; `origin_env` = portée de
        # l'album. Le bloc de portée n'a de sens que servi hors production.
        "env": APP_ENV,
        "origin_env": m.data.get("origin_env") or "prod",
        "has_editor_source": (src_dir / "preview.mp3").exists()
                              or (src_dir / "master.wav").exists(),
        "published": m.data.get("published", True),
        "per_track_covers": bool(album.get("per_track_covers", False)),
        # `source` (piste ajoutée depuis un lien) est exposée pour que la page
        # de gestion puisse la signaler : rien ne distingue sinon une
        # compilation d'un concert découpé.
        "tracks": [{"n": t.get("n"), "title": t.get("title"),
                    **({} if not t.get("artist") else {"artist": t.get("artist")}),
                    **({} if not t.get("source") else {"source": t.get("source")})}
                   for t in m.tracks],
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
    # Tournée + sous-titre bonus (facultatifs) — nettoyés si vides.
    for _f in ("tour", "subtitle"):
        _v = (getattr(payload, _f) or "").strip()
        if _v:
            alb[_f] = _v
        else:
            alb.pop(_f, None)
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


@app.get("/api/albums/{slug}/suggested-title")
def album_suggested_title(slug: str,
                          identity: dict = Depends(require_gestionnaire)) -> dict:
    """Titre suggéré au formalisme maison, pour pré-remplir le champ titre."""
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    from .titles import suggest_concert_title
    m = Manifest.load(path)
    return {"suggested_title": suggest_concert_title(m.data.get("album", {}))}


@app.get("/api/albums/{slug}/url-preview")
def album_url_preview(slug: str,
                      identity: dict = Depends(require_gestionnaire)) -> dict:
    """Slug canonique que donnerait « Corriger l'URL », sans rien modifier.
    Permet au front d'afficher/désactiver le bouton selon qu'il y a un gain."""
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(path)
    base = slugrename.clean_slug(m.data.get("album", {}))
    target = slugrename.unique_slug(base, slug) if base else ""
    return {"slug": slug, "target": target,
            "changed": bool(target) and target != slug,
            "reason": "" if base else "no_metadata"}


@app.post("/api/albums/{slug}/rename-url")
def album_rename_url(slug: str,
                     identity: dict = Depends(require_gestionnaire)) -> dict:
    """Corrige l'URL de l'album vers son slug canonique (artiste-date).
    Renomme le dossier, migre les données sociales, pose une redirection 301."""
    try:
        res = slugrename.rename_album(slug)
    except FileNotFoundError:
        raise HTTPException(404, "album introuvable")
    except Exception as exc:  # pragma: no cover - défensif
        raise HTTPException(500, f"échec du renommage : {exc}")
    return {"ok": True, **res}


@app.put("/api/albums/{slug}/tracks")
def update_tracks(slug: str, payload: TracksEditIn,
                  identity: dict = Depends(require_gestionnaire)) -> dict:
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(path)
    track_map = {t["n"]: t for t in m.tracks}
    new_tracks = []
    # Le `n` est renuméroté par **position** dans la liste reçue : c'est le
    # numéro de piste canonique (badge de la fiche publique, préfixe des
    # fichiers, tag TRCK). Réordonner sans renuméroter laissait le badge sur
    # l'ancien numéro alors que fichiers et tags suivaient déjà la position —
    # d'où l'affichage désordonné (02,03,…,01). On apparie chaque entrée à sa
    # piste existante par l'`n` d'origine (pour conserver start/end/source)
    # avant de réattribuer le nouveau numéro.
    for pos, ti in enumerate(payload.tracks, start=1):
        existing = dict(track_map.get(ti.n, {"start": None, "end": None, "locked": False}))
        existing["n"] = pos
        existing["title"] = ti.title
        new_tracks.append(existing)
    m.data["tracks"] = new_tracks
    m.save()

    # Tags ID3 par piste (titre + numéro), mapping fichier↔piste par titre
    # (robuste aux noms de fichiers hétérogènes), puis tags communs à l'album.
    tagged = _write_track_tags(slug, m)
    tagged += _write_album_tags(slug, m)
    renamed = _rename_audio_files(slug, m)
    # Un renommage de fichier ou un changement de titre ID3 laisse Jellyfin
    # avec l'ancien chemin/nom en cache : la lecture du fichier disparu
    # échoue côté client (« an error has occurred »). Best-effort, comme
    # à l'ajout de piste (cf. jellyfin.py).
    jellyfin.refresh_album(slug)
    return {"ok": True, "tracks": len(new_tracks), "mp3_tagged": tagged, "renamed": renamed}


@app.post("/api/albums/{slug}/cover")
async def upload_cover(slug: str, file: UploadFile = File(...),
                       identity: dict = Depends(require_gestionnaire)) -> dict:
    """Remplace la pochette « officielle » d'un album (outil de gestion).

    Historiquement, cette route écrivait un simple `artwork/cover.jpg` et
    mettait à jour `album.cover` dans le manifest — mais depuis l'ajout du
    système de propositions (table `covers`, servies par /cover-img/{id}), la
    fiche publique n'affichait plus jamais ce fichier, ce qui donnait au
    gestionnaire l'impression d'un enregistrement invisible.

    On aligne donc l'upload gestionnaire sur le pipeline « community » :
    l'image est stockée sous `artwork/covers/{key}_cover{ext}`, une ligne est
    insérée dans `covers` au nom du gestionnaire avec `pinned=1` (au plus une
    épinglée par album — on dépingle l'ancienne d'abord), puis
    `_on_covers_changed()` repointe le manifest vers cette nouvelle gagnante.
    Résultat : la vitrine (`/cover/{slug}`), la fiche publique
    (`/cover-img/{id}`) et les MP3 (`_write_album_cover`) reçoivent tous la
    même image, sans qu'un client ait besoin de vider son cache — un nouvel
    upload = nouvelle clé + nouvel id = nouvelles URLs.
    """
    if not _album_exists(slug):
        raise HTTPException(404, "album introuvable")
    path = PROJECTS_DIR / slug / "manifest.yaml"
    username = identity.get("username") or ""
    if not username:
        raise HTTPException(400, "identité manquante")
    cdata, cext = await _covers_read_upload(file, COVER_EXTS, COVER_MAX_BYTES, "pochette")
    key = uuid4().hex
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_conn() as conn:
        _ensure_profile(conn, username)
        # Une seule cover épinglée à la fois (index partiel dans le schéma).
        conn.execute("UPDATE covers SET pinned=0 WHERE slug=? AND pinned=1", (slug,))
        cur = conn.execute(
            "INSERT INTO covers(slug, username, file_key, cover_ext, traycard_ext, "
            "caption, created_at, updated_at, pinned) "
            "VALUES(?,?,?,?,?,?,?,?,1)",
            (slug, username, key, cext, "", "", now, now),
        )
        cover_id = cur.lastrowid
        covers_dir(slug).mkdir(parents=True, exist_ok=True)
        cover_file(slug, key, cext).write_bytes(cdata)
    # Repoint le manifest vers la nouvelle gagnante, puis nettoie le vieux
    # artwork/cover.* legacy (plus lu par personne une fois le manifest bougé).
    _on_covers_changed(slug)
    art_dir = PROJECTS_DIR / slug / "artwork"
    if art_dir.is_dir():
        for old in art_dir.glob("cover.*"):
            if old.is_file():
                old.unlink(missing_ok=True)
    m = Manifest.load(path)
    embedded = _write_album_cover(slug, m)
    return {"ok": True, "cover_id": cover_id,
            "cover": m.data.get("album", {}).get("cover"),
            "mp3_embedded": embedded}


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
    # Symlink Jellyfin (apparition/retrait dans Finamp) dès maintenant, plutôt
    # que d'attendre le prochain passage du cron sync-media.sh (jusqu'à 10 min).
    jellyfin.trigger_sync()
    # Publication → annonce aux abonnés (fan-out idempotent : une fois par env).
    notified = 0
    if payload.published:
        try:
            notified = notifications.announce_post(slug)
        except Exception:
            notified = 0
    return {"ok": True, "published": payload.published, "notified": notified}


@app.patch("/api/albums/bulk-published")
def set_published_bulk(payload: BulkPublishIn,
                       identity: dict = Depends(require_gestionnaire)) -> dict:
    """Publie/dépublie plusieurs albums d'un coup (niveau gestionnaire).

    Idempotent : un album déjà dans l'état visé n'est pas réécrit et ne renotifie
    pas. Les slugs inconnus sont ignorés et remontés dans `missing`.
    """
    slugs = list(dict.fromkeys(payload.slugs))
    updated, notified, missing = [], 0, []
    for slug in slugs:
        path = PROJECTS_DIR / slug / "manifest.yaml"
        if not path.exists():
            missing.append(slug)
            continue
        m = Manifest.load(path)
        if m.data.get("published", True) == payload.published:
            continue  # déjà dans l'état visé
        m.data["published"] = payload.published
        m.save(path)
        updated.append(slug)
        if payload.published:
            try:
                notified += notifications.announce_post(slug)
            except Exception as exc:
                log.warning("annonce de %s impossible : %s", slug, exc)
    if updated:
        jellyfin.trigger_sync()
    return {"ok": True, "published": payload.published,
            "updated": updated, "count": len(updated),
            "notified": notified, "missing": missing}


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
def album_detail_page(slug: str):
    # Ancien slug (album renommé) → redirection permanente vers l'URL actuelle,
    # pour ne pas casser les liens déjà partagés.
    if not (PROJECTS_DIR / slug / "manifest.yaml").exists():
        canon = slugrename.canonical_slug(slug)
        if canon != slug:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=f"/album/{canon}", status_code=301)
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


@app.get("/changelog", response_class=HTMLResponse)
def changelog() -> HTMLResponse:
    page = FRONTEND / "changelog.html"
    if page.exists():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>live2mp3 — changelog</h1>")


@app.get("/app/album/{slug}", response_class=HTMLResponse)
def album_admin_page(slug: str,
                     identity: dict = Depends(require_gestionnaire)):
    if not (PROJECTS_DIR / slug / "manifest.yaml").exists():
        canon = slugrename.canonical_slug(slug)
        if canon != slug:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=f"/app/album/{canon}", status_code=301)
    page = FRONTEND / "album.html"
    if page.exists():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>live2mp3 — gestion album</h1>")


@app.post("/api/jobs")
def create_job(job: JobIn,
               identity: dict = Depends(require_gestionnaire)) -> dict[str, Any]:
    multi = bool(job.clips)
    if multi:
        # Une piste par clip, dans l'ordre fourni. Les timecodes exacts sont
        # posés à la préparation (durées réelles des WAV décodés) : ici on ne
        # connaît que les durées sondées, insuffisamment précises pour couper.
        tracks = [{"n": i, "title": c.title.strip() or f"Piste {i}",
                   "artist": (c.artist or "").strip() or None,
                   "locked": True}
                  for i, c in enumerate(job.clips, start=1)]
        auto_setlist = False
    else:
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
        clips=[c.model_dump() for c in job.clips] if multi else None,
    )
    project_dir = PROJECTS_DIR / m.slug
    if (project_dir / "manifest.yaml").exists():
        raise HTTPException(
            409, f"un album existe déjà sous ce slug ({m.slug}) — "
                 "modifier l'artiste ou la date")
    m.data["auto_setlist"] = auto_setlist
    # Multi-liens : audio uniquement en V1 (concaténer des vidéos de formats
    # hétérogènes est un chantier distinct). La vidéo reste possible en mono-lien.
    m.data["source"]["media"] = "video" if (job.video and not multi) else "audio"
    if job.thumbnail_url:
        m.data["source"]["thumbnail_url"] = job.thumbnail_url
    if job.duration:
        m.data["source"]["duration"] = float(job.duration)
    m.data["meta"] = {
        "imported_by": identity.get("username") or "",
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "import_source": "url",
    }
    # Portée : un album créé hors production reste invisible du catalogue prod
    # (stockage partagé, cf. workspace/infra/stockage.md) jusqu'à sa promotion.
    # On n'écrit rien en prod : champ absent = album de production.
    if APP_ENV != "prod":
        m.data["origin_env"] = APP_ENV
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
    progress.reset(slug)
    _skip_detection.discard(slug)   # repart d'un état propre
    threading.Thread(target=_run_prepare_bg,
                     args=(slug, identity.get("username") or ""),
                     daemon=True).start()
    return {"ok": True, "slug": slug}


class AITrackIn(BaseModel):
    n: int = 0
    title: str
    artist: str | None = None


class PrepareAIIn(BaseModel):
    token: str            # staging de l'upload chunké
    file: str             # nom du fichier complet déposé
    artist: str
    title: str = ""
    date: str = ""
    venue: str = ""
    festival: str = ""
    setlistfm_url: str = ""
    target: str = "data_disc"
    # Setlist vérifiée dans le formulaire (prioritaire) ; vide → recherche
    # serveur (URL/date) puis, à défaut, découpe auto par l'IA.
    tracks: list[AITrackIn] | None = None


class SetlistLookupIn(BaseModel):
    artist: str = ""
    date: str = ""
    url: str = ""


@app.post("/api/import/setlist-lookup")
def import_setlist_lookup(payload: SetlistLookupIn,
                          identity: dict = Depends(require_gestionnaire)) -> dict:
    """Propose la setlist officielle (setlist.fm) pour pré-remplir le formulaire
    d'un import fichier : URL explicite prioritaire, sinon (artiste, date)."""
    try:
        if payload.url.strip():
            sl = setlistfm.lookup_by_url(payload.url.strip())
        elif payload.artist.strip() and payload.date.strip():
            sl = setlistfm.lookup(payload.artist.strip(), payload.date.strip())
        else:
            return {"found": False, "reason": "artiste et date requis (ou une URL)"}
    except setlistfm.SetlistUnavailable as e:
        return {"found": False, "reason": str(e)}
    if not sl:
        return {"found": False}
    return {"found": True, "url": sl.get("url", ""), "venue": sl.get("venue", ""),
            "tour": sl.get("tour", ""), "name": setlistfm.ATTRIBUTION,
            "tracks": [{"n": t["n"], "title": t["title"], "artist": t.get("artist")}
                       for t in sl["tracks"]]}


@app.post("/api/import/prepare-ai")
def prepare_ai(payload: PrepareAIIn,
               identity: dict = Depends(require_gestionnaire)) -> dict:
    """Crée un projet depuis un fichier complet déjà téléversé et lance la
    découpe IA. Réutilise le pipeline de l'import par lien (silences → Whisper →
    setlist.fm → DeepSeek) en sautant le téléchargement : le master est déjà là.

    La setlist officielle vient de l'URL setlist.fm fournie (prioritaire) ou
    d'une recherche (artiste, date) ; à défaut, l'IA devine les titres depuis la
    transcription (`auto_setlist`)."""
    from . import import_album as imp

    if not payload.artist.strip():
        raise HTTPException(400, "artiste requis")
    staging = imp._staging(payload.token)
    src_file = staging / "files" / Path(payload.file).name
    if not src_file.is_file():
        raise HTTPException(400, "fichier absent du dépôt")
    ext = src_file.suffix.lower()
    if ext not in imp.MEDIA_EXT:
        raise HTTPException(400, "format non pris en charge (audio ou vidéo)")
    is_video = ext in imp.VIDEO_EXT

    album = {"artist": payload.artist.strip(), "title": payload.title.strip(),
             "date": payload.date.strip(), "venue": payload.venue.strip(),
             "festival": payload.festival.strip()}

    setlist_url = payload.setlistfm_url.strip()
    setlist_source = "ai"
    if payload.tracks:
        # Pistes vérifiées dans le formulaire : prioritaires, aucune recherche.
        tracks = [{"n": t.n or i, "title": t.title.strip(),
                   "artist": (t.artist or None), "locked": False}
                  for i, t in enumerate(payload.tracks, start=1) if t.title.strip()]
        auto_setlist = not tracks
        if not tracks:
            tracks = [{"n": 1, "title": album["title"] or "Piste 1"}]
        else:
            setlist_source = "form"
    else:
        # Setlist officielle : URL explicite prioritaire, sinon (artiste, date).
        setlist = None
        try:
            if setlist_url:
                setlist = setlistfm.lookup_by_url(setlist_url)
            elif album["date"]:
                setlist = setlistfm.lookup(album["artist"], album["date"])
        except setlistfm.SetlistUnavailable:
            setlist = None
        if setlist:
            setlist_source = "setlistfm"
            setlist_url = setlist.get("url") or setlist_url
            tracks = [{"n": t["n"], "title": t["title"],
                       "artist": t.get("artist"), "locked": False}
                      for t in setlist["tracks"]]
            auto_setlist = False
            if not album["venue"] and setlist.get("venue"):
                album["venue"] = setlist["venue"]
            if not album["title"] and setlist.get("tour"):
                album["title"] = setlist["tour"]
        else:
            # Setlist inconnue : piste unique provisoire, l'IA la remplacera.
            tracks = [{"n": 1, "title": album["title"] or "Piste 1"}]
            auto_setlist = True
    if not album["title"]:
        album["title"] = f"{album['artist']} — Live"

    target = payload.target if payload.target in ("audio_cd", "data_disc") else "data_disc"
    m = new_manifest(album, tracks, target=target)
    project_dir = PROJECTS_DIR / m.slug
    if (project_dir / "manifest.yaml").exists():
        raise HTTPException(409, f"un album existe déjà sous ce slug ({m.slug}) — "
                                 "modifier l'artiste ou la date")
    m.data["auto_setlist"] = auto_setlist
    m.data["source"]["media"] = "video" if is_video else "audio"
    m.data["published"] = False
    if APP_ENV != "prod":
        m.data["origin_env"] = APP_ENV
    m.data["meta"] = {
        "imported_by": identity.get("username") or "",
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "import_source": "upload",
    }
    if setlist_url:
        m.data["meta"]["setlistfm_url"] = setlist_url

    source_dir = project_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    try:
        # Le fichier téléversé devient le master (déplacement sur le même volume).
        if is_video:
            master = project_dir / m.data["source"]["master_mkv"]
        else:
            master = source_dir / f"master_audio{ext}"
            m.data["source"]["master_audio"] = f"source/master_audio{ext}"
        shutil.move(str(src_file), str(master))
        # Extraction WAV lossless : étape commune, remplace ce que ferait download.
        wav = project_dir / m.data["source"]["master_wav"]
        download.extract_wav(master, wav)
        m.set_state("download", "done")
        m.data["source"]["duration"] = download._wav_seconds(wav)
        m.save(project_dir / "manifest.yaml")
    except Exception:
        shutil.rmtree(project_dir, ignore_errors=True)
        raise

    shutil.rmtree(staging, ignore_errors=True)

    # Comme create_job : on ne lance PAS la préparation ici. Le front enchaîne
    # sur POST /api/jobs/{slug}/prepare (même parcours que l'import par lien),
    # ce qui évite toute course sur le flux d'événements SSE.
    return {"ok": True, "slug": m.slug, "media": m.data["source"]["media"],
            "setlist_source": setlist_source,
            "auto_setlist": auto_setlist, "tracks": len(tracks)}


class ReorderIn(BaseModel):
    order: list[int]   # index 0-based des pistes actuelles, dans le nouvel ordre


@app.post("/api/jobs/{slug}/reorder")
def reorder_tracks(slug: str, payload: ReorderIn,
                   identity: dict = Depends(require_gestionnaire)) -> dict:
    """Réordonne un album multi-liens : re-concatène le master dans le nouvel
    ordre, recalcule les timecodes, puis régénère forme d'onde et preview.

    Synchrone (rapide : découpe/concat d'un WAV déjà local) ; l'éditeur recharge
    ensuite le manifeste à jour. Réservé aux albums multi-liens.
    """
    project_dir = PROJECTS_DIR / slug
    if not (project_dir / "manifest.yaml").exists():
        raise HTTPException(404, "projet introuvable")
    try:
        res = download.reorder_master(project_dir, payload.order)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Régénère les dérivés du master pour que l'éditeur reflète le nouvel ordre.
    m = Manifest.load(project_dir / "manifest.yaml")
    wav = project_dir / m.data["source"]["master_wav"]
    preanalyze.generate_waveform(wav, project_dir / "source" / "waveform.dat")
    (project_dir / "source" / "preview.mp3").unlink(missing_ok=True)
    linktool.make_preview(project_dir, m)
    return {"ok": True, **res}


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
    cut_tracks = [{
        "n": i,
        "title": t.title.strip(),
        **({"artist": t.artist.strip()} if t.artist and t.artist.strip() else {}),
        "start": float(t.start),
        "end": float(t.end),
        "locked": True,
    } for i, t in enumerate(tracks, start=1)]
    # L'éditeur ne voit que le découpage du master : les pistes ajoutées
    # depuis un lien (addtrack) sont recollées en fin de liste, sinon cette
    # écriture les effacerait purement et simplement.
    m.data["tracks"] = addtrack.preserve_external_tracks(slug, m, cut_tracks)
    m.data["auto_setlist"] = False
    m.save()
    _rename_audio_files(slug, m)
    # Réconcilie Jellyfin avec les fichiers renommés (cf. update_tracks).
    jellyfin.refresh_album(slug)
    return {"ok": True, "tracks": len(m.tracks)}


def _is_app_admin(identity: dict) -> bool:
    """Admin applicatif ou superuser Authentik (peut agir sur le bien d'autrui)."""
    if identity.get("is_admin"):
        return True
    groups = set(identity.get("groups") or set())
    return bool(groups & ({GROUP_APP_ADMIN} | SUPERUSER_GROUPS))


def _may_delete_draft(identity: dict, owner: str) -> bool:
    """Un gestionnaire ne supprime que ses propres brouillons ; l'admin, tous.

    Un brouillon sans `imported_by` (import historique) est traité comme
    appartenant à personne : seul un admin peut le nettoyer.
    """
    if _is_app_admin(identity):
        return True
    return bool(owner) and owner == identity.get("username")


@app.get("/api/drafts")
def list_drafts(identity: dict = Depends(require_gestionnaire)) -> dict:
    """Imports interrompus, tous gestionnaires confondus.

    `can_delete` est calculé côté serveur pour que le front n'ait pas à
    dupliquer la règle de droits (bouton corbeille grisé si faux) — la
    vérification faisant autorité reste celle de `DELETE /api/jobs/{slug}`.
    """
    drafts = catalogue.list_drafts()
    owners = {d["imported_by"] for d in drafts if d["imported_by"]}
    profiles: dict[str, dict] = {}
    if owners:
        with get_conn() as conn:
            profiles = _profiles_map(conn, owners)
    for d in drafts:
        u = d["imported_by"]
        prof = profiles.get(u) or {}
        d["owner"] = {
            "username": u,
            "display_name": prof.get("display_name") or u,
            "avatar": bool(prof.get("avatar")),
        } if u else None
        d["can_delete"] = _may_delete_draft(identity, u)
    return {"drafts": drafts, "is_admin": _is_app_admin(identity),
            "username": identity.get("username") or ""}


@app.get("/app/drafts", response_class=HTMLResponse)
def drafts_page() -> HTMLResponse:
    page = FRONTEND / "drafts.html"
    if page.exists():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>live2mp3 — brouillons</h1>")


@app.delete("/api/jobs/{slug}")
def delete_job(slug: str,
               identity: dict = Depends(require_gestionnaire)) -> dict:
    """Supprime un brouillon (projet jamais rendu). Refus si l'album existe."""
    project_dir = PROJECTS_DIR / slug
    mpath = project_dir / "manifest.yaml"
    if not mpath.exists():
        raise HTTPException(404, "projet introuvable")
    if any((project_dir / "build" / "audio").glob("*.mp3")):
        raise HTTPException(409, "album déjà rendu — suppression refusée")
    # Un gestionnaire ne peut jeter que son propre brouillon : le travail d'un
    # collègue (téléchargement + détection déjà payés) ne doit pas disparaître
    # sur un clic. L'admin reste seul juge pour le ménage global.
    try:
        owner = (Manifest.load(mpath).data.get("meta", {}) or {}).get("imported_by", "")
    except Exception:
        owner = ""
    if not _may_delete_draft(identity, owner):
        raise HTTPException(403, "brouillon d'un autre gestionnaire")
    linktool.delete_project_social(slug)
    shutil.rmtree(project_dir, ignore_errors=True)
    progress.reset(slug)
    return {"ok": True}


@app.post("/api/albums/{slug}/promote")
def promote_album(slug: str,
                  identity: dict = Depends(require_gestionnaire)) -> dict:
    """Rend visible en production un album créé en preprod.

    Aucun fichier n'est déplacé : prod et preprod partagent le même stockage,
    seule l'étiquette de portée disparaît. L'opération est donc atomique et
    réversible (cf. `demote`), là où une copie de ~1,5 Go par album exposerait
    une fenêtre d'échec partiel.
    """
    if APP_ENV == "prod":
        raise HTTPException(400, "déjà en production")
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(path)
    if not m.data.get("origin_env"):
        raise HTTPException(400, "cet album est déjà visible en production")
    m.data.pop("origin_env", None)
    m.save()
    return {"ok": True, "slug": slug, "origin_env": "prod"}


@app.post("/api/albums/{slug}/demote")
def demote_album(slug: str,
                 identity: dict = Depends(require_gestionnaire)) -> dict:
    """Retire un album de la production (retour à la portée preprod).

    Filet de la promotion : une erreur se corrige en remettant l'étiquette,
    sans toucher aux médias.
    """
    if APP_ENV == "prod":
        raise HTTPException(400, "opération réservée à la preprod")
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(path)
    m.data["origin_env"] = APP_ENV
    m.save()
    return {"ok": True, "slug": slug, "origin_env": APP_ENV}


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


@app.post("/api/jobs/{slug}/render")
def start_render(slug: str, media: str = "audio", gap: float = 2.0,
                 video: bool | None = None,
                 identity: dict = Depends(require_gestionnaire)) -> dict:
    """Met le rendu en file (worker RQ). Refuse un doublon sur le même album.

    Le rendu tournait avant dans un thread de ce process : ffmpeg disputait ses
    cœurs à l'API, un redémarrage tuait le job, et rien n'empêchait deux rendus
    concurrents d'écrire les mêmes fichiers.
    """
    project_dir = PROJECTS_DIR / slug
    path = project_dir / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "projet introuvable")
    m = Manifest.load(path)
    if video is None:
        # Clips MP4 seulement si la source vidéo a été téléchargée.
        video = m.data.get("source", {}).get("media", "video") == "video"
    # Un album déjà publié qu'on ré-édite (cf. bouton « Ouvrir l'éditeur audio »
    # sur la fiche de gestion) doit forcer le re-rendu : le pipeline est
    # idempotent par nom de fichier, donc une piste dont le nom ne change pas
    # garderait sinon son ancien découpage. En création, published est encore
    # False à ce stade → comportement idempotent existant inchangé.
    republish = bool(m.data.get("published"))
    progress.reset(slug)
    try:
        renderqueue.enqueue(slug, media=media, gap=gap, video=video,
                            republish=republish,
                            requested_by=identity.get("username", ""))
    except renderqueue.DuplicateRender as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "slug": slug}


@app.post("/api/jobs/{slug}/render/cancel")
def cancel_render(slug: str,
                  identity: dict = Depends(require_gestionnaire)) -> dict:
    """Arrête un rendu en file ou en cours.

    Le ffmpeg courant est tué et son fichier partiel supprimé (cf.
    pipeline/render.py) : un rendu relancé ensuite repart proprement.
    """
    kind = renderqueue.active_kind(slug)
    state = renderqueue.cancel(slug, kind)
    if state == "none":
        raise HTTPException(404, "aucun rendu en cours pour cet album")
    if state in ("queued", "deferred"):
        # Retiré de la file : personne ne publiera l'évènement final, l'API
        # s'en charge pour que l'interface réagisse tout de suite.
        progress.publish(slug, {
            "stage": "video_all" if kind == renderqueue.KIND_VIDEO else "all",
            "status": "cancelled", "info": {}})
    return {"ok": True, "was": state, "kind": kind}


class QueueOrderIn(BaseModel):
    slugs: list[str]


@app.get("/api/render-queue")
def render_queue(identity: dict = Depends(require_gestionnaire)) -> dict:
    """File des rendus : l'actif en tête, puis les suivants dans l'ordre."""
    items = []
    for it in renderqueue.listing():
        m = None
        mpath = PROJECTS_DIR / it["slug"] / "manifest.yaml"
        if mpath.exists():
            try:
                m = Manifest.load(mpath).data
            except Exception:
                m = None
        album = (m or {}).get("album", {})
        # Les formats décrivent ce que *ce* job produit : la phase 2 ne rend
        # que le MP4, l'annoncer « mp3 + mp4 » laisserait croire que l'album
        # audio n'est pas encore là alors qu'il est déjà écoutable.
        kind = it.get("kind", renderqueue.KIND_RENDER)
        if kind == renderqueue.KIND_VIDEO:
            formats = ["mp4"]
        else:
            formats = ["mp3", "mp4"] if it.get("video") else ["mp3"]
        items.append({
            **it,
            "kind": kind,
            "artist": album.get("artist", ""),
            "title": album.get("title", "") or it["slug"],
            "formats": formats,
        })
    return {"items": items}


@app.post("/api/render-queue/reorder")
def render_queue_reorder(payload: QueueOrderIn,
                         identity: dict = Depends(require_gestionnaire)) -> dict:
    return {"ok": True, "order": renderqueue.reorder(payload.slugs)}


@app.post("/api/jobs/{slug}/render/pause")
def pause_render(slug: str, paused: bool = True,
                 identity: dict = Depends(require_gestionnaire)) -> dict:
    """Gèle (SIGSTOP) ou relance (SIGCONT) le rendu en cours.

    Rien n'est perdu : le process reprend exactement où il s'était arrêté.
    """
    kind = renderqueue.active_kind(slug)
    if kind is None:
        raise HTTPException(404, "aucun rendu en cours pour cet album")
    renderqueue.set_paused(slug, paused, kind)
    progress.publish(slug, {"stage": "video" if kind == renderqueue.KIND_VIDEO
                            else "render",
                            "status": "paused" if paused else "running",
                            "info": {"paused": paused}})
    return {"ok": True, "paused": paused, "kind": kind}


@app.get("/api/jobs/{slug}/events")
def events(slug: str,
           identity: dict = Depends(require_gestionnaire)) -> StreamingResponse:
    """Flux SSE de progression."""
    def gen():
        # Rejeu de l'historique : un client qui arrive en cours de rendu, ou qui
        # revient après un aller-retour dans l'interface, retrouve l'état exact.
        seen_final = False
        for ev in progress.history(slug):
            yield f"data: {json.dumps(ev)}\n\n"
            if ev.get("status") in ("complete", "error", "cancelled"):
                seen_final = True
        if seen_final:
            return
        for ev in progress.stream(slug):
            if ev is None:
                yield ": keepalive\n\n"
                continue
            yield f"data: {json.dumps(ev)}\n\n"
            if ev.get("status") in ("complete", "error", "cancelled"):
                break
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/jobs/{slug}/bundle")
def download_bundle(slug: str,
                    identity: dict = Depends(require_gestionnaire)) -> FileResponse:
    """ZIP assemblé à la demande, puis supprimé une fois servi.

    Il n'est plus produit par le pipeline ni conservé : du MP3/MP4 étant déjà
    compressé, le ZIP ne gagnait rien et doublait l'occupation disque de chaque
    album (13 Go de doublons relevés le 2026-07-30). Construit sur disque et
    non en mémoire — un bundle dépasse couramment 4 Go.
    """
    project_dir = PROJECTS_DIR / slug
    manifest_path = project_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise HTTPException(404, "projet introuvable")
    if not any((project_dir / "build" / "audio").glob("*.mp3")):
        raise HTTPException(404, "aucun média à télécharger")

    stem = download_stem(Manifest.load(manifest_path).data, slug)
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"bundle-{slug}-"))
    try:
        out = bundle.run(project_dir, out_zip=tmp_dir / "bundle.zip")["bundle"]
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return FileResponse(
        out, filename=f"{stem}.zip", media_type="application/zip",
        background=BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True),
    )


@app.get("/api/jobs/{slug}/waveform.dat")
def waveform(slug: str,
             identity: dict = Depends(require_gestionnaire)) -> FileResponse:
    path = PROJECTS_DIR / slug / "source" / "waveform.dat"
    if not path.exists():
        raise HTTPException(404, "waveform absente")
    return FileResponse(path, media_type="application/octet-stream")


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
