"""Pochettes multiples : import, attribution, likes, tray card liée.

Un album n'a plus « une » pochette mais une collection de propositions, chacune
créditée à son auteur. Celle qui s'affiche est **résolue à la lecture** par
`rank_covers` (épinglée par un gestionnaire > la plus likée > la plus ancienne)
et n'est jamais stockée : un like ne réécrit rien, il change le classement.

La tray card n'est pas une entité : c'est une colonne de la cover qui la porte
(`traycard_ext`). Une tray card orpheline est donc impossible par construction,
et supprimer une cover emporte la sienne.

Métadonnées en SQLite (concurrent, voir `db.py`), fichiers sur le volume albums
sous `projects/<slug>/artwork/covers/`. Le manifest garde `album.cover` comme
pointeur legacy vers la gagnante, pour les consommateurs qui lisent le YAML.
"""
from __future__ import annotations

import io
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from mutagen.id3 import ID3
from .albumfiles import MIME_EXT, _file_track_n
from .auth import current_identity, require_gestionnaire, require_user
from .db import _APP_ENV as APP_ENV, get_conn
from .manifest import PROJECTS_DIR, Manifest
from .printable import cover_pdf, traycard_pdf
from . import thumbs
from .social import (
    _album_exists,
    _album_visible,
    _ensure_profile,
    _is_moderator,
    _profiles_map,
)

log = logging.getLogger(__name__)

router = APIRouter()

COVER_MAX_BYTES = 15 * 1024 * 1024
TRAYCARD_MAX_BYTES = 25 * 1024 * 1024
CAPTION_MAX = 300

# Formats acceptés à l'import (images uniquement — le PDF imprimable est
# généré à la volée au téléchargement).
COVER_EXTS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
TRAYCARD_EXTS = {**COVER_EXTS}

MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".pdf": "application/pdf",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def covers_dir(slug: str) -> Path:
    return PROJECTS_DIR / slug / "artwork" / "covers"


# Types d'image gérés par cette collection : la pochette d'album historique
# ('cover') et les 3 visuels Jellyfin d'un concert vidéo. Chaque type est une
# collection indépendante (tri, épinglage, suppression) partageant le même code.
KINDS = ("cover", "banner", "poster", "thumbnail")
IMAGE_KINDS = ("banner", "poster", "thumbnail")


def cover_file(slug: str, file_key: str, ext: str, kind: str = "cover") -> Path:
    return covers_dir(slug) / f"{file_key}_{kind}{ext}"


def traycard_file(slug: str, file_key: str, ext: str) -> Path:
    return covers_dir(slug) / f"{file_key}_traycard{ext}"


def _slug_token(name: str) -> str:
    """Jeton sûr pour un nom de fichier de téléchargement (anti-traversée)."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", name or "")[:60] or "anon"


# --- Classement ------------------------------------------------------------
def rank_covers(conn: sqlite3.Connection, slug: str,
                kind: str = "cover") -> list[sqlite3.Row]:
    """Covers d'un album, la gagnante en tête.

    Ordre : épinglée d'abord (au plus une, garantie par index partiel), puis
    les pochettes manuelles avant l'auto (une miniature d'import ne doit jamais
    l'emporter sur une pochette proposée à la main), puis likes décroissants,
    puis la plus ancienne — départage stable, donc le rang utilisé pour nommer
    les fichiers du ZIP ne bouge pas sans raison.
    """
    return conn.execute(
        "SELECT c.*, COUNT(l.cover_id) AS likes "
        "FROM covers c LEFT JOIN cover_likes l ON l.cover_id = c.id "
        "WHERE c.slug=? AND c.kind=? GROUP BY c.id "
        "ORDER BY c.pinned DESC, c.auto ASC, likes DESC, c.created_at ASC, c.id ASC",
        (slug, kind),
    ).fetchall()


def owns_shared_files(data: dict) -> bool:
    """Cet environnement peut-il écrire les fichiers partagés de l'album ?

    La prod possède tous les albums sauf ceux créés en preprod et pas encore
    poussés (`origin_env`) ; la preprod ne possède que ces derniers.
    """
    origin = data.get("origin_env") or "prod"
    return origin == APP_ENV


def top_cover(conn: sqlite3.Connection, slug: str,
              kind: str = "cover") -> sqlite3.Row | None:
    rows = rank_covers(conn, slug, kind)
    return rows[0] if rows else None


def _on_covers_changed(slug: str, *, kind: str = "cover",
                       force: bool = False) -> None:
    """Répercute un changement de classement sur le reste du système.

    On repointe `album.cover` du manifest vers la gagnante : c'est ce que lisent
    le catalogue et les consommateurs du YAML. Le ZIP reste volontairement
    *paresseux* (`_zip_media` le rafraîchit au téléchargement).

    Propagation vers le média servi à Jellyfin/Finamp (ré-embarquement APIC) :
    - par défaut (`force=False`, cas des likes fréquents), seulement si la
      gagnante *change réellement* — optimisation, la propagation est rare ;
    - `force=True` pour les actions de gestion explicites (import, épinglage,
      suppression) : on propage **même si le pointeur `album.cover` est
      inchangé**. Indispensable car l'APIC embarqué peut diverger du pointeur
      (ex. pochette posée *avant* le rendu, ou embarquée par un rendu antérieur).
      Sans ça, épingler/supprimer une cover ne rafraîchit jamais ce que voient
      Finamp/Jellyfin (bug 2026-08-13).
    """
    mpath = PROJECTS_DIR / slug / "manifest.yaml"
    if not mpath.exists():
        return
    from .manifest import Manifest

    m = Manifest.load(mpath)
    if not owns_shared_files(m.data):
        # Manifest et médias sont PARTAGÉS entre prod et preprod, la base
        # sociale ne l'est pas : un like ou un import en preprod réécrivait
        # le manifest (et les APIC) de la prod avec la gagnante *preprod*,
        # d'où vitrine ≠ fiche en prod (incident 2026-09-26). Chaque
        # environnement lit désormais sa gagnante en base (`/cover/{slug}`).
        return
    with get_conn() as conn:
        win = top_cover(conn, slug, kind)
    album = m.data.setdefault("album", {})
    # Pointeur legacy dans le manifest : `album.cover` pour la pochette,
    # `album.banner`/`poster`/`thumbnail` pour les visuels Jellyfin. Permet aux
    # consommateurs du YAML (catalogue, fiche, sync Jellyfin) de lire la gagnante.
    rel = (
        f"artwork/covers/{win['file_key']}_{kind}{win['cover_ext']}" if win else None
    )
    changed = album.get(kind) != rel
    if changed:
        if rel:
            album[kind] = rel
        else:
            album.pop(kind, None)
        # Repointage technique (un like de pochette suffit à changer la
        # gagnante) : ce n'est pas une modification de contenu de l'album.
        # Avec `touch`, un simple like faisait apparaitre « Mis a jour ».
        m.save(touch=False)
    if changed or force:
        if kind == "cover":
            _propagate_cover_to_media(slug, m)
        else:
            _propagate_images_to_jellyfin(slug, m)


def _propagate_cover_to_media(slug: str, m) -> None:
    """Répercute la pochette d'album vers les fichiers servis à Jellyfin.

    - album à pochette unique → ré-embarque `album.cover` en APIC dans toutes
      les pistes (vignette d'album et pistes cohérentes) ;
    - album `per_track_covers` → dépose seulement une image de dossier
      (`cover.jpg`), **sans toucher aux APIC des pistes** qui ont chacune leur
      propre pochette ;
    puis force Jellyfin à ré-extraire les images. Best-effort : ne lève jamais
    (une pochette non propagée ne doit pas casser un like/upload).
    """
    if not m.data.get("published", True):
        return
    audio_dir = PROJECTS_DIR / slug / "build" / "audio"
    if not audio_dir.exists():
        return  # album pas encore rendu : rien à propager
    from . import albumfiles, jellyfin

    try:
        if m.data.get("album", {}).get("per_track_covers"):
            albumfiles._write_folder_cover(slug, m)
        else:
            albumfiles._write_album_cover(slug, m)
    except Exception as exc:
        # La pochette est un confort : un échec ne doit pas bloquer la
        # republication, mais il doit laisser une trace.
        log.warning("écriture de la pochette de %s impossible : %s", slug, exc)
    jellyfin.refresh_album(slug)


# Suffixes de nommage des images sidecar reconnus par Jellyfin pour un fichier
# vidéo « <nom>.mp4 » : poster (Primary), miniature paysage (Thumb 16:9),
# bannière (Banner). Déposés à côté du MP4 concert-complet.
JELLYFIN_IMG_SUFFIX = {"poster": "-poster", "thumbnail": "-thumb", "banner": "-banner"}


def _propagate_images_to_jellyfin(slug: str, m) -> None:
    """Dépose les visuels gagnants (bannière/poster/miniature) en images sidecar
    à côté du MP4 concert-complet (build/video-full), aux conventions Jellyfin,
    puis déclenche un scan. Best-effort : ne lève jamais.
    """
    vfdir = PROJECTS_DIR / slug / "build" / "video-full"
    mp4s = sorted(vfdir.glob("*.mp4")) if vfdir.exists() else []
    if not mp4s:
        return  # pas de concert complet à illustrer
    stem = mp4s[0].with_suffix("").name
    from . import jellyfin
    try:
        with get_conn() as conn:
            for kind in IMAGE_KINDS:
                suffix = JELLYFIN_IMG_SUFFIX[kind]
                # Purge des anciens sidecars de ce type (toutes extensions).
                # `list(...)` : on supprime pendant le parcours, il faut donc
                # matérialiser l'inventaire avant de toucher au répertoire.
                for old in list(vfdir.iterdir()):
                    if old.name.startswith(f"{stem}{suffix}."):
                        old.unlink(missing_ok=True)
                win = top_cover(conn, slug, kind)
                if win:
                    src = cover_file(slug, win["file_key"], win["cover_ext"], kind)
                    if src.exists():
                        (vfdir / f"{stem}{suffix}{win['cover_ext']}").write_bytes(
                            src.read_bytes())
    except Exception as exc:
        log.warning("écriture des visuels Jellyfin de %s impossible : %s", slug, exc)
    jellyfin.refresh_album(slug)


def _ensure_auto_thumbnail(slug: str) -> None:
    """Crée une miniature « auto » (frame 16:9 1920×1080 extraite du concert
    complet) si l'album a une vidéo complète et aucune miniature encore. Marquée
    auto=1 : un import manuel passe aussitôt devant (même règle que les covers).
    Best-effort : ne lève jamais.
    """
    import subprocess
    vfdir = PROJECTS_DIR / slug / "build" / "video-full"
    mp4s = sorted(vfdir.glob("*.mp4")) if vfdir.exists() else []
    if not mp4s:
        return
    with get_conn() as conn:
        if conn.execute(
            "SELECT 1 FROM covers WHERE slug=? AND kind='thumbnail' LIMIT 1", (slug,)
        ).fetchone():
            return
    key = uuid4().hex
    out = cover_file(slug, key, ".jpg", "thumbnail")
    covers_dir(slug).mkdir(parents=True, exist_ok=True)
    try:
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(mp4s[0])],
            capture_output=True, text=True).stdout.strip() or 0)
        ts = max(1.0, dur * 0.33)  # frame au premier tiers (évite l'écran noir d'intro)
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(ts), "-i", str(mp4s[0]), "-frames:v", "1",
             "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080",
             str(out)], check=True, capture_output=True)
    except Exception as exc:
        log.warning("miniature auto de %s impossible : %s", slug, exc)
        return
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO covers(slug, username, file_key, cover_ext, traycard_ext, "
            "caption, kind, auto, created_at, updated_at) VALUES(?,?,?,?,?,?,?,1,?,?)",
            (slug, "auto", key, ".jpg", "", "", "thumbnail", now, now),
        )
    _on_covers_changed(slug, kind="thumbnail", force=True)


_COVERS_REL = re.compile(r"^artwork/covers/([0-9a-f]{32})_cover(\.[a-z0-9]+)$")


def _ensure_auto_cover(slug: str) -> None:
    """Matérialise la pochette du manifest en vraie ligne `covers` « auto »
    quand l'album n'en a aucune dans cet environnement.

    Sans ça, la fiche retombait sur une pseudo-pochette `id:0` : sans id, pas
    de likes, pas de commentaires, pas d'épinglage — et un clic n'ouvrait que
    l'agrandissement au lieu de la fiche pochette. Le cas est structurel, pas
    accidentel : **les projets sont sur un volume partagé prod/preprod mais la
    base sociale est scindée par environnement** (`.l2m-social/<APP_ENV>/`),
    donc tout album importé ailleurs (ou avant la table `covers`) arrive ici
    sans ligne. Créer la ligne à la première lecture referme le trou pour tous
    les albums, présents et futurs.

    - Si le manifest pointe déjà dans `artwork/covers/` (pochette passée par le
      pipeline dans un autre environnement), on **réutilise la clé existante** :
      aucun fichier n'est dupliqué.
    - Sinon la source est copiée sous une clé **déterministe** (dérivée du
      chemin), pour que les deux environnements convergent sur le même fichier.
    - `auto=1` : un import manuel passe aussitôt devant, comme pour la
      miniature automatique.
    - On ne touche NI au manifest NI aux médias : c'est une matérialisation de
      ce qui est déjà affiché, pas un changement de pochette. Repointer le
      manifest ré-embarquerait les APIC de toutes les pistes sur le volume
      partagé, à la simple consultation d'une fiche.

    Best-effort : ne lève jamais.
    """
    with get_conn() as conn:
        if conn.execute(
            "SELECT 1 FROM covers WHERE slug=? AND kind='cover' LIMIT 1", (slug,)
        ).fetchone():
            return
    mpath = PROJECTS_DIR / slug / "manifest.yaml"
    if not mpath.exists():
        return
    try:
        rel = ((Manifest.load(mpath).data.get("album") or {}).get("cover") or "")
    except Exception as exc:
        log.warning("pochette auto de %s : manifest illisible (%s)", slug, exc)
        return
    if not rel:
        return
    src = PROJECTS_DIR / slug / rel
    if not src.is_file():
        return
    ext = src.suffix.lower()
    if ext not in set(COVER_EXTS.values()):
        return

    m = _COVERS_REL.match(rel)
    if m:
        key = m.group(1)                       # déjà dans le pipeline : on réutilise
    else:
        import hashlib
        key = hashlib.sha1(f"{slug}/{rel}".encode()).hexdigest()[:32]
        dst = cover_file(slug, key, ext)
        if not dst.exists():
            try:
                covers_dir(slug).mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
            except Exception as exc:
                log.warning("pochette auto de %s impossible : %s", slug, exc)
                return
    now = _now()
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO covers(slug, username, file_key, cover_ext, "
                "traycard_ext, caption, kind, auto, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,'cover',1,?,?)",
                (slug, "auto", key, ext, "", "", now, now),
            )
    except Exception as exc:
        log.warning("pochette auto de %s non enregistrée : %s", slug, exc)


def zip_basename(rank: int, username: str) -> str:
    """Préfixe commun cover/tray card dans le ZIP : `01-nathan`.

    Rang d'abord : le tri alphabétique du ZIP reproduit le classement par
    popularité (la gagnante en 01) et garde chaque paire cover+traycard
    adjacente, puisque seul le suffixe les distingue.
    """
    return f"{rank:02d}-{_slug_token(username)}"


# --- Sérialisation ---------------------------------------------------------
def _cover_dict(row: sqlite3.Row, rank: int, profiles: dict[str, dict],
                liked: set[int], comment_counts: dict[int, int]) -> dict:
    u = row["username"]
    prof = profiles.get(u, {"display_name": u, "avatar": False})
    return {
        "id": row["id"],
        "slug": row["slug"],
        "rank": rank,
        "username": u,
        "display_name": prof["display_name"],
        "avatar": prof["avatar"],
        "caption": row["caption"] or "",
        "pinned": bool(row["pinned"]),
        # Pochette récupérée automatiquement (miniature d'import) : le front
        # affiche « Pochette automatique » au lieu de « Pochette par X ».
        "auto": bool(row["auto"]),
        "likes": row["likes"],
        "liked": row["id"] in liked,
        "comments": comment_counts.get(row["id"], 0),
        "has_traycard": bool(row["traycard_ext"]),
        # Le front rend un PDF en <iframe> et une image en <img> : l'URL seule ne
        # permet pas de trancher (pas d'extension), d'où ce type explicite.
        "traycard_kind": ("pdf" if row["traycard_ext"] == ".pdf" else "image")
                         if row["traycard_ext"] else None,
        "cover_url": f"/cover-img/{row['id']}",
        # Dérivés légers pour les grilles et le carrousel — `cover_url` reste
        # l'original (lightbox, impression, téléchargement).
        "cover_thumb": f"/cover-img/{row['id']}?w=320",
        "cover_medium": f"/cover-img/{row['id']}?w=640",
        "traycard_url": f"/traycard-img/{row['id']}" if row["traycard_ext"] else None,
        "download_basename": zip_basename(rank, u),
        "created_at": row["created_at"],
    }


def _list_payload(conn: sqlite3.Connection, slug: str, viewer: str | None,
                  kind: str = "cover") -> dict:
    rows = rank_covers(conn, slug, kind)
    ids = [r["id"] for r in rows]
    liked: set[int] = set()
    counts: dict[int, int] = {}
    if ids:
        qs = ",".join("?" * len(ids))
        if viewer:
            liked = {
                r["cover_id"]
                for r in conn.execute(
                    f"SELECT cover_id FROM cover_likes WHERE username=? "
                    f"AND cover_id IN ({qs})",
                    (viewer, *ids),
                )
            }
        counts = {
            r["cover_id"]: r["n"]
            for r in conn.execute(
                f"SELECT cover_id, COUNT(*) AS n FROM comments "
                f"WHERE deleted=0 AND cover_id IN ({qs}) GROUP BY cover_id",
                tuple(ids),
            )
        }
    profiles = _profiles_map(conn, {r["username"] for r in rows})
    return {
        "slug": slug,
        "covers": [
            _cover_dict(r, i + 1, profiles, liked, counts)
            for i, r in enumerate(rows)
        ],
    }


def _get_cover(conn: sqlite3.Connection, cover_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM covers WHERE id=?", (cover_id,)).fetchone()
    if not row:
        raise HTTPException(404, "pochette introuvable")
    return row


def _may_edit(row: sqlite3.Row, identity: dict) -> bool:
    return row["username"] == identity.get("username") or _is_moderator(
        set(identity.get("groups") or [])
    )


async def _read_upload(file: UploadFile, allowed: dict[str, str],
                       max_bytes: int, what: str) -> tuple[bytes, str]:
    """Valide taille + format, retourne (contenu, extension normalisée)."""
    raw = await file.read()
    if not raw:
        raise HTTPException(400, f"{what} vide")
    if len(raw) > max_bytes:
        raise HTTPException(413, f"{what} trop lourde (max {max_bytes // (1024*1024)} Mo)")
    ext = allowed.get(file.content_type or "")
    if not ext:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in set(allowed.values()):
            raise HTTPException(400, f"format {what} non supporté (JPEG, PNG, WebP"
                                     f"{', PDF' if '.pdf' in allowed.values() else ''})")
    # Le content-type est déclaratif : on vérifie que les images s'ouvrent
    # vraiment, sinon un fichier arbitraire finirait servi et embarqué en APIC.
    if ext != ".pdf":
        try:
            from PIL import Image
            Image.open(io.BytesIO(raw)).verify()
        except Exception:
            raise HTTPException(400, f"{what} : image illisible")
    elif not raw.startswith(b"%PDF"):
        raise HTTPException(400, "tray card : PDF invalide")
    return raw, ext


# =====================================================================
#  LECTURE
# =====================================================================
@router.get("/api/social/albums/{slug}/covers")
def list_covers(slug: str, identity: dict = Depends(current_identity)) -> dict:
    if not _album_visible(slug, identity):
        raise HTTPException(404, "album introuvable")
    _ensure_auto_cover(slug)   # pochette du manifest → vraie ligne (voir docstring)
    with get_conn() as conn:
        return _list_payload(conn, slug, identity.get("username"))


def _serve(cover_id: int, what: str, download: bool,
           width: int = 0) -> FileResponse:
    """Sert l'image (`what="image"`, selon le type de la ligne : cover/banner/
    poster/thumbnail) ou sa tray card (`what="traycard"`, cover uniquement)."""
    with get_conn() as conn:
        row = _get_cover(conn, cover_id)
    if what == "traycard":
        ext = row["traycard_ext"]
        if not ext:
            raise HTTPException(404, "pas de tray card")
        path = traycard_file(row["slug"], row["file_key"], ext)
        label = "traycard"
    else:
        ext = row["cover_ext"]
        path = cover_file(row["slug"], row["file_key"], ext, row["kind"])
        label = row["kind"]
    if not path.exists():
        raise HTTPException(404, "fichier absent")
    name = None
    if download:
        name = f"{row['slug']}-{_slug_token(row['username'])}_{label}{ext}"
    # Un téléchargement rend toujours l'original : la vignette n'est là que pour
    # l'affichage en grille (cf. thumbs.py).
    derived = thumbs.derive(path, width) if (width and not download) else None
    if derived:
        return FileResponse(derived, media_type="image/webp", headers={
            "Cache-Control": "public, max-age=31536000, immutable",
        })
    return FileResponse(path, media_type=MEDIA_TYPES.get(ext, "application/octet-stream"),
                        filename=name)


@router.get("/cover-img/{cover_id}")
def get_cover_img(cover_id: int, w: int = 0) -> FileResponse:
    return _serve(cover_id, "image", download=False, width=w)


@router.get("/traycard-img/{cover_id}")
def get_traycard_img(cover_id: int, w: int = 0) -> FileResponse:
    return _serve(cover_id, "traycard", download=False, width=w)


@router.get("/download/cover/{cover_id}")
def download_one_cover(cover_id: int,
                       identity: dict = Depends(require_user)) -> FileResponse:
    return _serve(cover_id, "image", download=True)


@router.get("/download/traycard/{cover_id}")
def download_one_traycard(cover_id: int,
                          identity: dict = Depends(require_user)) -> FileResponse:
    return _serve(cover_id, "traycard", download=True)


def _serve_pdf(cover_id: int, kind: str) -> Response:
    with get_conn() as conn:
        row = _get_cover(conn, cover_id)
    ext = row["cover_ext"] if kind == "cover" else row["traycard_ext"]
    if not ext:
        raise HTTPException(404, "fichier absent")
    fn = cover_file if kind == "cover" else traycard_file
    path = fn(row["slug"], row["file_key"], ext)
    if not path.exists():
        raise HTTPException(404, "fichier absent")
    gen = cover_pdf if kind == "cover" else traycard_pdf
    data = gen(path)
    name = f"{row['slug']}-{_slug_token(row['username'])}_{kind}_print.pdf"
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/download/cover/{cover_id}/pdf")
def download_cover_pdf(cover_id: int,
                       identity: dict = Depends(require_user)) -> Response:
    return _serve_pdf(cover_id, "cover")


@router.get("/download/traycard/{cover_id}/pdf")
def download_traycard_pdf(cover_id: int,
                          identity: dict = Depends(require_user)) -> Response:
    return _serve_pdf(cover_id, "traycard")


# =====================================================================
#  ÉCRITURE
# =====================================================================
@router.post("/api/social/albums/{slug}/covers")
async def upload_cover(slug: str,
                       cover: UploadFile = File(...),
                       traycard: UploadFile | None = File(default=None),
                       caption: str = Form(default=""),
                       identity: dict = Depends(require_user)) -> dict:
    """Import d'une proposition : cover obligatoire, tray card optionnelle."""
    if not _album_exists(slug):
        raise HTTPException(404, "album introuvable")
    username = identity["username"]
    cdata, cext = await _read_upload(cover, COVER_EXTS, COVER_MAX_BYTES, "pochette")
    tdata, text = b"", ""
    # `traycard` non renseigné arrive soit à None, soit en part vide selon le client.
    if traycard is not None and (traycard.filename or ""):
        tdata, text = await _read_upload(
            traycard, TRAYCARD_EXTS, TRAYCARD_MAX_BYTES, "tray card"
        )
    now = _now()
    # Clé de fichier tirée avant l'insert : elle doit être unique sur le volume
    # partagé prod+preprod, donc surtout pas dérivée de l'`id` local.
    key = uuid4().hex
    with get_conn() as conn:
        _ensure_profile(conn, username)
        cur = conn.execute(
            "INSERT INTO covers(slug, username, file_key, cover_ext, traycard_ext, "
            "caption, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (slug, username, key, cext, text, caption.strip()[:CAPTION_MAX], now, now),
        )
        cover_id = cur.lastrowid
        # Écriture disque dans la transaction : si elle échoue, le rollback
        # évite une ligne qui pointerait vers un fichier inexistant.
        covers_dir(slug).mkdir(parents=True, exist_ok=True)
        cover_file(slug, key, cext).write_bytes(cdata)
        if text:
            traycard_file(slug, key, text).write_bytes(tdata)
        payload = _list_payload(conn, slug, username)
    _on_covers_changed(slug, force=True)
    return {"ok": True, "cover_id": cover_id, **payload}


# --- Visuels Jellyfin (bannière / poster / miniature) ----------------------
# Mêmes collection, tri, épinglage, likes et suppression que les covers : seul
# le `kind` change. Les routes like/pin/delete (par cover_id) sont partagées.
def _check_image_kind(kind: str) -> None:
    if kind not in IMAGE_KINDS:
        raise HTTPException(404, "type d'image inconnu")


@router.get("/api/social/albums/{slug}/images/{kind}")
def list_images(slug: str, kind: str,
                identity: dict = Depends(current_identity)) -> dict:
    _check_image_kind(kind)
    if not _album_visible(slug, identity):
        raise HTTPException(404, "album introuvable")
    if kind == "thumbnail":
        _ensure_auto_thumbnail(slug)  # miniature auto (frame) si vidéo + aucune encore
    with get_conn() as conn:
        return {"kind": kind, **_list_payload(conn, slug, identity.get("username"), kind)}


@router.post("/api/social/albums/{slug}/images/{kind}")
async def upload_image(slug: str, kind: str,
                       cover: UploadFile = File(...),
                       caption: str = Form(default=""),
                       identity: dict = Depends(require_user)) -> dict:
    """Import d'un visuel Jellyfin (bannière/poster/miniature) — sans tray card."""
    _check_image_kind(kind)
    if not _album_exists(slug):
        raise HTTPException(404, "album introuvable")
    username = identity["username"]
    cdata, cext = await _read_upload(cover, COVER_EXTS, COVER_MAX_BYTES, "image")
    now = _now()
    key = uuid4().hex
    with get_conn() as conn:
        _ensure_profile(conn, username)
        cur = conn.execute(
            "INSERT INTO covers(slug, username, file_key, cover_ext, traycard_ext, "
            "caption, kind, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (slug, username, key, cext, "", caption.strip()[:CAPTION_MAX], kind, now, now),
        )
        cover_id = cur.lastrowid
        covers_dir(slug).mkdir(parents=True, exist_ok=True)
        cover_file(slug, key, cext, kind).write_bytes(cdata)
        payload = _list_payload(conn, slug, username, kind)
    _on_covers_changed(slug, kind=kind, force=True)
    return {"ok": True, "cover_id": cover_id, "kind": kind, **payload}


@router.put("/api/social/covers/{cover_id}/traycard")
async def set_traycard(cover_id: int, traycard: UploadFile = File(...),
                       identity: dict = Depends(require_user)) -> dict:
    """Ajoute ou remplace la tray card d'une cover existante.

    Pas de route de création de tray card seule : elle n'existe que rattachée.
    """
    with get_conn() as conn:
        row = _get_cover(conn, cover_id)
        if not _may_edit(row, identity):
            raise HTTPException(403, "pochette d'un autre utilisateur")
        # Une tray card n'a de sens que sur une pochette d'album : sur une
        # bannière/poster/miniature, elle serait invisible et le classement
        # recalculé porterait sur la mauvaise collection.
        if row["kind"] != "cover":
            raise HTTPException(409, "tray card réservée aux pochettes d'album")
        data, ext = await _read_upload(
            traycard, TRAYCARD_EXTS, TRAYCARD_MAX_BYTES, "tray card"
        )
        old = row["traycard_ext"]
        if old and old != ext:
            traycard_file(row["slug"], row["file_key"], old).unlink(missing_ok=True)
        covers_dir(row["slug"]).mkdir(parents=True, exist_ok=True)
        traycard_file(row["slug"], row["file_key"], ext).write_bytes(data)
        conn.execute(
            "UPDATE covers SET traycard_ext=?, updated_at=? WHERE id=?",
            (ext, _now(), cover_id),
        )
        payload = _list_payload(conn, row["slug"], identity.get("username"))
    _on_covers_changed(row["slug"])
    return {"ok": True, **payload}


@router.delete("/api/social/covers/{cover_id}/traycard")
def delete_traycard(cover_id: int, identity: dict = Depends(require_user)) -> dict:
    with get_conn() as conn:
        row = _get_cover(conn, cover_id)
        if not _may_edit(row, identity):
            raise HTTPException(403, "pochette d'un autre utilisateur")
        if row["kind"] != "cover":
            raise HTTPException(409, "tray card réservée aux pochettes d'album")
        if row["traycard_ext"]:
            traycard_file(row["slug"], row["file_key"], row["traycard_ext"]).unlink(missing_ok=True)
        conn.execute(
            "UPDATE covers SET traycard_ext='', updated_at=? WHERE id=?",
            (_now(), cover_id),
        )
        payload = _list_payload(conn, row["slug"], identity.get("username"))
    _on_covers_changed(row["slug"])
    return {"ok": True, **payload}


@router.delete("/api/albums/{slug}/cover")
def delete_manifest_cover(slug: str, identity: dict = Depends(require_gestionnaire)) -> dict:
    """Supprime la pochette de manifeste (album.cover) — gestionnaires uniquement.

    Appelé quand l'utilisateur supprime une cover virtuelle id=0, c'est-à-dire
    une pochette qui n'est enregistrée qu'en YAML (ancienne couche, avant la
    collection sociale), pas en base de données.
    """
    mpath = PROJECTS_DIR / slug / "manifest.yaml"
    if not mpath.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(mpath)
    cover_rel = m.data.get("album", {}).get("cover")
    if cover_rel:
        (PROJECTS_DIR / slug / cover_rel).unlink(missing_ok=True)
        m.data.get("album", {}).pop("cover", None)
        m.save()
    _propagate_cover_to_media(slug, m)
    with get_conn() as conn:
        payload = _list_payload(conn, slug, identity.get("username"))
    return {"ok": True, **payload}


@router.delete("/api/albums/{slug}/cover/auto")
def delete_album_cover_auto(slug: str, identity: dict = Depends(require_gestionnaire)) -> dict:
    """Supprime la pochette courante de l'album, quelle que soit sa source.

    Depuis la page de gestion (album.html), un gestionnaire veut retirer la
    pochette affichée. On cherche dans l'ordre : pochette gagnante en DB (même
    source que /cover/{slug}), puis cover de manifeste. Les deux sont nettoyées
    pour garantir la cohérence partout (accueil, profil, carousel).
    """
    mpath = PROJECTS_DIR / slug / "manifest.yaml"
    if not mpath.exists():
        raise HTTPException(404, "album introuvable")

    # 1. Supprimer les pochettes (kind='cover' uniquement — les visuels Jellyfin
    #    banner/poster/thumbnail ont leur propre gestion) en DB + fichiers.
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM covers WHERE slug=? AND kind='cover'", (slug,)
        ).fetchall()
        for row in rows:
            cover_file(slug, row["file_key"], row["cover_ext"]).unlink(missing_ok=True)
            if row["traycard_ext"]:
                traycard_file(slug, row["file_key"], row["traycard_ext"]).unlink(missing_ok=True)
        if rows:
            conn.execute("DELETE FROM covers WHERE slug=? AND kind='cover'", (slug,))

    # 2. Nettoyer le manifest
    m = Manifest.load(mpath)
    cover_rel = m.data.get("album", {}).get("cover")
    if cover_rel:
        (PROJECTS_DIR / slug / cover_rel).unlink(missing_ok=True)
        m.data.get("album", {}).pop("cover", None)
        m.save()
    _propagate_cover_to_media(slug, m)

    return {"ok": True, "has_cover": False}


@router.delete("/api/social/covers/{cover_id}")
def delete_cover(cover_id: int, identity: dict = Depends(require_user)) -> dict:
    """Supprime une cover, sa tray card et ses commentaires (ON DELETE CASCADE)."""
    with get_conn() as conn:
        row = _get_cover(conn, cover_id)
        if not _may_edit(row, identity):
            raise HTTPException(403, "pochette d'un autre utilisateur")
        slug = row["slug"]
        kind = row["kind"]
        cover_file(slug, row["file_key"], row["cover_ext"], kind).unlink(missing_ok=True)
        if row["traycard_ext"]:
            traycard_file(slug, row["file_key"], row["traycard_ext"]).unlink(missing_ok=True)
        conn.execute("DELETE FROM covers WHERE id=?", (cover_id,))
        payload = _list_payload(conn, slug, identity.get("username"), kind)
    _on_covers_changed(slug, kind=kind, force=True)
    return {"ok": True, **payload}


@router.post("/api/social/covers/{cover_id}/like")
def toggle_cover_like(cover_id: int, identity: dict = Depends(require_user)) -> dict:
    username = identity["username"]
    with get_conn() as conn:
        row = _get_cover(conn, cover_id)
        _ensure_profile(conn, username)
        existing = conn.execute(
            "SELECT 1 FROM cover_likes WHERE cover_id=? AND username=?",
            (cover_id, username),
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM cover_likes WHERE cover_id=? AND username=?",
                (cover_id, username),
            )
        else:
            conn.execute(
                "INSERT INTO cover_likes(cover_id, username, created_at) VALUES(?,?,?)",
                (cover_id, username, _now()),
            )
        payload = _list_payload(conn, row["slug"], username, row["kind"])
    # Un like peut faire changer la gagnante -> APIC/visuels et ZIP à rafraîchir.
    _on_covers_changed(row["slug"], kind=row["kind"])
    return {"ok": True, **payload}


@router.post("/api/social/covers/{cover_id}/pin")
def pin_cover(cover_id: int, identity: dict = Depends(require_gestionnaire)) -> dict:
    """Épingle une cover (bascule). Une épinglée passe devant les likes."""
    with get_conn() as conn:
        row = _get_cover(conn, cover_id)
        slug = row["slug"]
        kind = row["kind"]
        now = _now()
        # Dépingler d'abord : l'index partiel n'autorise qu'une épinglée par
        # (album, type), poser la nouvelle avant de retirer l'ancienne échouerait.
        conn.execute(
            "UPDATE covers SET pinned=0, updated_at=? WHERE slug=? AND kind=? AND pinned=1",
            (now, slug, kind),
        )
        if not row["pinned"]:
            conn.execute(
                "UPDATE covers SET pinned=1, updated_at=? WHERE id=?", (now, cover_id)
            )
        payload = _list_payload(conn, slug, identity.get("username"), kind)
    _on_covers_changed(slug, kind=kind, force=True)
    return {"ok": True, **payload}


# =====================================================================
#  POCHETTES PAR PISTE
# =====================================================================

def track_covers_dir(slug: str) -> Path:
    return PROJECTS_DIR / slug / "artwork" / "track-covers"


def track_cover_file(slug: str, file_key: str, ext: str) -> Path:
    return track_covers_dir(slug) / f"{file_key}{ext}"


def _get_track_cover(conn, tc_id: int):
    row = conn.execute("SELECT * FROM track_covers WHERE id=?", (tc_id,)).fetchone()
    if not row:
        raise HTTPException(404, "pochette introuvable")
    return row


@router.get("/track-cover/{tc_id}")
def get_track_cover_img(tc_id: int) -> FileResponse:
    with get_conn() as conn:
        row = _get_track_cover(conn, tc_id)
    path = track_cover_file(row["slug"], row["file_key"], row["cover_ext"])
    if not path.exists():
        raise HTTPException(404, "fichier absent")
    return FileResponse(path, media_type=MEDIA_TYPES.get(row["cover_ext"], "application/octet-stream"))


@router.get("/api/albums/{slug}/track-covers")
def list_track_covers(slug: str, identity: dict = Depends(require_gestionnaire)) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM track_covers WHERE slug=? ORDER BY track_n", (slug,)
        ).fetchall()
    return {
        "slug": slug,
        "track_covers": [
            {"id": r["id"], "track_n": r["track_n"], "username": r["username"],
             "cover_url": f"/track-cover/{r['id']}", "created_at": r["created_at"]}
            for r in rows
        ],
    }


@router.post("/api/albums/{slug}/tracks/{n}/cover")
async def upload_track_cover(
    slug: str, n: int,
    file: UploadFile = File(...),
    identity: dict = Depends(require_gestionnaire),
) -> dict:
    """Import ou remplacement de la pochette d'une piste (gestionnaire, sans tray card)."""
    if not _album_exists(slug):
        raise HTTPException(404, "album introuvable")
    username = identity["username"]
    cdata, cext = await _read_upload(file, COVER_EXTS, COVER_MAX_BYTES, "pochette")
    now = _now()
    key = uuid4().hex
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM track_covers WHERE slug=? AND track_n=?", (slug, n)
        ).fetchone()
        track_covers_dir(slug).mkdir(parents=True, exist_ok=True)
        if existing:
            old = track_cover_file(slug, existing["file_key"], existing["cover_ext"])
            old.unlink(missing_ok=True)
            track_cover_file(slug, key, cext).write_bytes(cdata)
            conn.execute(
                "UPDATE track_covers SET file_key=?, cover_ext=?, username=?, updated_at=? "
                "WHERE slug=? AND track_n=?",
                (key, cext, username, now, slug, n),
            )
            tc_id = existing["id"]
        else:
            track_cover_file(slug, key, cext).write_bytes(cdata)
            cur = conn.execute(
                "INSERT INTO track_covers(slug, track_n, username, file_key, cover_ext, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (slug, n, username, key, cext, now, now),
            )
            tc_id = cur.lastrowid
        rows = conn.execute(
            "SELECT id, track_n FROM track_covers WHERE slug=? ORDER BY track_n", (slug,)
        ).fetchall()
    return {
        "ok": True,
        "track_cover_id": tc_id,
        "track_covers": [
            {"id": r["id"], "track_n": r["track_n"], "cover_url": f"/track-cover/{r['id']}"}
            for r in rows
        ],
    }


@router.delete("/api/track-covers/{tc_id}")
def delete_track_cover(tc_id: int, identity: dict = Depends(require_gestionnaire)) -> dict:
    with get_conn() as conn:
        row = _get_track_cover(conn, tc_id)
        track_cover_file(row["slug"], row["file_key"], row["cover_ext"]).unlink(missing_ok=True)
        conn.execute("DELETE FROM track_covers WHERE id=?", (tc_id,))
        rows = conn.execute(
            "SELECT id, track_n FROM track_covers WHERE slug=? ORDER BY track_n",
            (row["slug"],),
        ).fetchall()
    return {
        "ok": True,
        "track_covers": [
            {"id": r["id"], "track_n": r["track_n"], "cover_url": f"/track-cover/{r['id']}"}
            for r in rows
        ],
    }


@router.post("/api/albums/{slug}/sync-track-covers")
def sync_track_covers_from_apic(
    slug: str, identity: dict = Depends(require_gestionnaire)
) -> dict:
    """Synchronise les pochettes individuelles depuis les tags APIC embarqués dans chaque MP3."""
    if not _album_exists(slug):
        raise HTTPException(404, "album introuvable")
    audio_dir = PROJECTS_DIR / slug / "build" / "audio"
    if not audio_dir.exists():
        raise HTTPException(404, "pas de pistes audio")

    username = identity["username"]
    now = _now()
    synced = 0
    errors: list[str] = []

    for mp3_path in sorted(audio_dir.glob("*.mp3")):
        file_n = _file_track_n(mp3_path.stem)
        if file_n is None:
            continue
        try:
            tags = ID3(str(mp3_path))
        except Exception:
            errors.append(mp3_path.name)
            continue
        apics = tags.getall("APIC")
        if not apics:
            continue
        apic = apics[0]
        ext = MIME_EXT.get(apic.mime, ".jpg")
        key = uuid4().hex
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM track_covers WHERE slug=? AND track_n=?", (slug, file_n)
            ).fetchone()
            track_covers_dir(slug).mkdir(parents=True, exist_ok=True)
            if existing:
                old_f = track_cover_file(slug, existing["file_key"], existing["cover_ext"])
                old_f.unlink(missing_ok=True)
                track_cover_file(slug, key, ext).write_bytes(apic.data)
                conn.execute(
                    "UPDATE track_covers SET file_key=?, cover_ext=?, username=?, updated_at=? "
                    "WHERE slug=? AND track_n=?",
                    (key, ext, username, now, slug, file_n),
                )
            else:
                track_cover_file(slug, key, ext).write_bytes(apic.data)
                conn.execute(
                    "INSERT INTO track_covers(slug, track_n, username, file_key, cover_ext, "
                    "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                    (slug, file_n, username, key, ext, now, now),
                )
        synced += 1

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, track_n FROM track_covers WHERE slug=? ORDER BY track_n", (slug,)
        ).fetchall()

    return {
        "ok": True,
        "synced": synced,
        "errors": errors,
        "track_covers": [
            {"id": r["id"], "track_n": r["track_n"], "cover_url": f"/track-cover/{r['id']}"}
            for r in rows
        ],
    }
