"""Import d'un album déjà découpé — dépôt de fichiers ou ZIP depuis le site.

Parcours en deux temps :
1. `POST /api/import/analyze` : les fichiers (MP3/images, en vrac ou dans un
   ZIP) sont déposés dans un staging, les métadonnées ID3 sont lues et
   renvoyées au front pour pré-remplir le formulaire.
2. `POST /api/import/commit` : les valeurs (éventuellement corrigées par le
   gestionnaire) créent le projet, déplacent les fichiers et écrivent
   manifest + tags + pochette.

Le staging vit sous `projects/.l2m-import/<token>` : même volume que les
projets, donc le commit est un simple déplacement (pas de copie inter-disques)
et rien ne transite par /tmp du conteneur.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from mutagen import MutagenError
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from pydantic import BaseModel

from . import jellyfin
from .albumfiles import (
    MIME_EXT,
    _sanitize_filename,
    _write_album_cover,
    _write_album_tags,
    _write_track_tags,
)
from .auth import require_gestionnaire
from .covers import (
    COVER_EXTS,
    TRAYCARD_EXTS,
    _now,
    _on_covers_changed,
    cover_file,
    covers_dir,
    traycard_file,
)
from .db import get_conn
from .manifest import PROJECTS_DIR, Manifest, new_manifest, project_slug
from .social import _ensure_profile

router = APIRouter(prefix="/api/import", tags=["import"])

STAGING_TTL_S = 6 * 3600  # un staging abandonné est purgé au bout de 6 h


def _staging_root() -> Path:
    """Racine des stagings — résolue à l'appel, pour suivre PROJECTS_DIR (tests)."""
    return PROJECTS_DIR / ".l2m-import"

AUDIO_EXT = {".mp3"}
# Vidéo acceptée à l'import (concert complet ou clips pré-découpés). Le MP4 est
# le cas courant ; mkv/webm/mov/m4v sont tolérés (yt-dlp/ffmpeg les gèrent).
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
ZIP_EXT = {".zip"}
# Extensions acceptées par l'upload chunké (gros fichiers). Les images passent
# encore par le multipart classique de `/analyze` (petites).
MEDIA_EXT = AUDIO_EXT | VIDEO_EXT

MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024  # 4 Go par dépôt multipart (audio/zip)
# La vidéo passe par l'upload chunké : un concert complet dépasse volontiers 4 Go.
MAX_VIDEO_BYTES = 16 * 1024 * 1024 * 1024
# Taille d'un chunk imposée côté serveur comme plafond : Cloudflare rejette les
# corps > 100 Mo. Le client découpe en morceaux plus petits (~8 Mo) ; on refuse
# tout chunk qui dépasserait cette marge de sécurité.
MAX_CHUNK_BYTES = 90 * 1024 * 1024
MAX_FILES = 400

# Fichiers parasites des exports macOS / Windows.
_JUNK_RE = re.compile(r"(^\.|^__MACOSX/|/\._|Thumbs\.db$|\.DS_Store$)")

# Noms de fichiers image qui désignent explicitement une pochette.
_COVER_HINT_RE = re.compile(r"(cover|folder|front|album|pochette|artwork)", re.I)
_TRAYCARD_HINT_RE = re.compile(r"(tray|back|verso|inlay)", re.I)


class TrackIn(BaseModel):
    n: int
    title: str
    file: str


class CommitIn(BaseModel):
    token: str
    artist: str
    title: str
    date: str = ""
    venue: str = ""
    festival: str = ""
    source_url: str = ""
    source_label: str = ""
    target: str = "audio_cd"
    published: bool = False
    cover: str = ""      # nom de fichier staging, ou "" si aucune
    traycard: str = ""
    tracks: list[TrackIn]


class UploadInitIn(BaseModel):
    filename: str
    size: int
    token: str | None = None   # pour ajouter un fichier à un staging existant


class UploadFinishIn(BaseModel):
    size: int


class StagedIn(BaseModel):
    token: str


# ── Staging ────────────────────────────────────────────────────────────────

def _staging(token: str) -> Path:
    """Résout un token en dossier de staging, en refusant toute évasion de chemin."""
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise HTTPException(400, "token invalide")
    d = _staging_root() / token
    if not d.is_dir():
        raise HTTPException(404, "import expiré ou introuvable")
    return d


def _purge_stale_stagings() -> None:
    root = _staging_root()
    if not root.exists():
        return
    now = time.time()
    for d in root.iterdir():
        try:
            if d.is_dir() and now - d.stat().st_mtime > STAGING_TTL_S:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _safe_member_name(name: str) -> str | None:
    """Nom de fichier sûr pour un membre de ZIP (anti zip-slip), ou None si à ignorer."""
    if _JUNK_RE.search(name) or name.endswith("/"):
        return None
    # On aplatit l'arborescence : seul le nom de fichier compte.
    base = Path(name.replace("\\", "/")).name
    if not base or base in (".", "..") or base.startswith("."):
        return None
    ext = Path(base).suffix.lower()
    if ext not in AUDIO_EXT | IMAGE_EXT:
        return None
    return _sanitize_filename(base) or None


def _unique_path(d: Path, name: str) -> Path:
    p = d / name
    if not p.exists():
        return p
    stem, ext = Path(name).stem, Path(name).suffix
    for i in range(2, 1000):
        p = d / f"{stem} ({i}){ext}"
        if not p.exists():
            return p
    raise HTTPException(400, "trop de fichiers homonymes")


# ── Lecture des métadonnées ────────────────────────────────────────────────

def _tag1(tags: Any, key: str) -> str:
    try:
        v = tags.get(key)
    except Exception:
        return ""
    if not v:
        return ""
    if isinstance(v, list):
        v = v[0] if v else ""
    return str(v).strip()


def _parse_track_n(name: str) -> int | None:
    m = re.match(r"^\s*(\d{1,3})\s*[-._)\s]", name)
    return int(m.group(1)) if m else None


def _title_from_filename(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"^\s*\d{1,3}\s*[-._)\s]+", "", stem)
    stem = stem.replace("_", " ")
    return re.sub(r"\s+", " ", stem).strip() or Path(name).stem


def _read_mp3(path: Path) -> dict[str, Any]:
    """Métadonnées d'un MP3 : tags ID3 si présents, sinon repli sur le nom de fichier."""
    info: dict[str, Any] = {
        "file": path.name, "title": "", "artist": "", "album": "",
        "albumartist": "", "date": "", "n": None, "duration": 0.0,
        "has_embedded_cover": False,
    }
    try:
        audio = MP3(str(path))
        info["duration"] = float(audio.info.length or 0)
    except (MutagenError, OSError):
        pass
    try:
        tags = ID3(str(path))
    except (MutagenError, OSError):
        tags = None
    if tags is not None:
        info["title"] = _tag1(tags, "TIT2")
        info["artist"] = _tag1(tags, "TPE1")
        info["album"] = _tag1(tags, "TALB")
        info["albumartist"] = _tag1(tags, "TPE2")
        info["date"] = _tag1(tags, "TDRC") or _tag1(tags, "TYER")
        trck = _tag1(tags, "TRCK")
        if trck:
            m = re.match(r"^\s*(\d+)", trck)
            if m:
                info["n"] = int(m.group(1))
        info["has_embedded_cover"] = bool(tags.getall("APIC"))
    if info["n"] is None:
        info["n"] = _parse_track_n(path.name)
    if not info["title"]:
        info["title"] = _title_from_filename(path.name)
    return info


def _read_video(path: Path) -> dict[str, Any]:
    """Métadonnées d'un fichier vidéo : durée via ffprobe, titre depuis le nom.

    Un MP4 de concert n'a pas de tags ID3 exploitables comme un MP3 ; artiste,
    date et titres viendront du formulaire (import manuel) ou de l'IA/setlist.fm.
    """
    info: dict[str, Any] = {
        "file": path.name, "title": "", "artist": "", "album": "",
        "albumartist": "", "date": "", "n": None, "duration": 0.0,
        "has_embedded_cover": False, "media": "video",
    }
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        info["duration"] = float((out.stdout or "").strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    info["n"] = _parse_track_n(path.name)
    info["title"] = _title_from_filename(path.name)
    return info


def _read_media(path: Path) -> dict[str, Any]:
    """Lecture d'un média du staging (MP3 → tags ID3, vidéo → ffprobe)."""
    if path.suffix.lower() in VIDEO_EXT:
        return _read_video(path)
    info = _read_mp3(path)
    info["media"] = "audio"
    return info


def _most_common(values: list[str]) -> str:
    """Valeur non vide la plus fréquente — les tags d'un album sont rarement unanimes."""
    vals = [v for v in values if v]
    if not vals:
        return ""
    return max(set(vals), key=vals.count)


def _extract_staging_cover(files_dir: Path, tracks: list[dict]) -> str | None:
    """Extrait l'APIC du premier MP3 qui en a un vers le staging. Retourne le nom."""
    for t in tracks:
        if not t["has_embedded_cover"]:
            continue
        try:
            tags = ID3(str(files_dir / t["file"]))
            apic = tags.getall("APIC")[0]
        except Exception:
            continue
        ext = MIME_EXT.get(apic.mime, ".jpg")
        out = _unique_path(files_dir, f"cover{ext}")
        out.write_bytes(apic.data)
        return out.name
    return None


# ── Endpoints ──────────────────────────────────────────────────────────────

# ── Upload chunké résumable (gros fichiers audio/vidéo) ──────────────────────
# Cloudflare rejette les corps > 100 Mo : un concert MP4 de plusieurs Go ne peut
# pas partir en une requête. Le client découpe le fichier en morceaux (~8 Mo),
# envoyés séquentiellement ; le serveur les appende sur disque (jamais en RAM) et
# expose l'octet déjà reçu pour reprendre après une coupure réseau (4G).

def _incoming(staging: Path, file_id: str) -> tuple[Path, dict]:
    if not re.fullmatch(r"[0-9a-f]{32}", file_id or ""):
        raise HTTPException(400, "file_id invalide")
    inc = staging / "incoming"
    part, metaf = inc / f"{file_id}.part", inc / f"{file_id}.json"
    if not part.exists() or not metaf.exists():
        raise HTTPException(404, "upload introuvable ou expiré")
    return part, json.loads(metaf.read_text())


@router.post("/upload/init")
def upload_init(payload: UploadInitIn,
                identity: dict = Depends(require_gestionnaire)) -> dict:
    """Ouvre (ou rejoint) un staging et réserve un fichier à téléverser en chunks."""
    _purge_stale_stagings()
    ext = Path(payload.filename or "").suffix.lower()
    if ext not in MEDIA_EXT:
        raise HTTPException(400, "format non accepté (audio ou vidéo attendu)")
    if payload.size <= 0 or payload.size > MAX_VIDEO_BYTES:
        raise HTTPException(413, "fichier trop volumineux (max 16 Go)")
    if payload.token:
        staging = _staging(payload.token)   # valide le token et son existence
        token = payload.token
    else:
        token = uuid.uuid4().hex
        staging = _staging_root() / token
        (staging / "files").mkdir(parents=True, exist_ok=True)
    inc = staging / "incoming"
    inc.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex
    meta = {
        "filename": Path(payload.filename).name,
        "ext": ext,
        "size": int(payload.size),
        "sanitized": _sanitize_filename(Path(payload.filename).stem) or "media",
    }
    (inc / f"{file_id}.json").write_text(json.dumps(meta))
    (inc / f"{file_id}.part").touch()
    return {"token": token, "file_id": file_id, "received": 0}


@router.get("/upload/{token}/{file_id}")
def upload_status(token: str, file_id: str,
                  identity: dict = Depends(require_gestionnaire)) -> dict:
    """Octet déjà reçu — permet au client de reprendre un upload interrompu."""
    staging = _staging(token)
    part, meta = _incoming(staging, file_id)
    return {"received": part.stat().st_size, "size": meta["size"],
            "filename": meta["filename"]}


@router.put("/upload/{token}/{file_id}")
async def upload_chunk(token: str, file_id: str, request: Request,
                       identity: dict = Depends(require_gestionnaire)) -> dict:
    """Appende un chunk à l'offset annoncé. Refuse un offset désynchronisé (409
    avec l'octet réellement reçu) pour que le client se recale sans corrompre."""
    staging = _staging(token)
    part, meta = _incoming(staging, file_id)
    try:
        offset = int(request.headers.get("X-Chunk-Offset", ""))
    except ValueError:
        raise HTTPException(400, "en-tête X-Chunk-Offset manquant ou invalide")
    cur = part.stat().st_size
    if offset != cur:
        # Le client doit reprendre à partir de `received` : ni trou ni doublon.
        raise HTTPException(409, {"error": "offset désynchronisé", "received": cur})
    written = 0
    with part.open("r+b") as fh:
        fh.seek(offset)
        async for chunk in request.stream():
            written += len(chunk)
            if written > MAX_CHUNK_BYTES:
                raise HTTPException(413, "chunk trop volumineux (max 90 Mo)")
            if cur + written > meta["size"]:
                raise HTTPException(413, "dépasse la taille annoncée")
            fh.write(chunk)
    return {"received": part.stat().st_size}


@router.post("/upload/{token}/{file_id}/finish")
def upload_finish(token: str, file_id: str, payload: UploadFinishIn,
                  identity: dict = Depends(require_gestionnaire)) -> dict:
    """Clôt un upload : vérifie la taille et déplace le fichier dans le staging."""
    staging = _staging(token)
    files_dir = staging / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    part, meta = _incoming(staging, file_id)
    size = part.stat().st_size
    if size != meta["size"] or size != payload.size:
        raise HTTPException(400, f"upload incomplet ({size}/{meta['size']} octets)")
    if len(list(files_dir.iterdir())) >= MAX_FILES:
        raise HTTPException(413, f"trop de fichiers (max {MAX_FILES})")
    dest = _unique_path(files_dir, f"{meta['sanitized']}{meta['ext']}")
    shutil.move(str(part), str(dest))
    (staging / "incoming" / f"{file_id}.json").unlink(missing_ok=True)
    return {"token": token, "file": dest.name, "size": size}


@router.post("/analyze")
async def analyze(files: list[UploadFile] = File(...),
                  identity: dict = Depends(require_gestionnaire)) -> dict:
    """Dépose les fichiers en staging et propose un pré-remplissage du formulaire."""
    _purge_stale_stagings()
    if not files:
        raise HTTPException(400, "aucun fichier")

    token = uuid.uuid4().hex
    files_dir = _staging_root() / token / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    total = 0
    try:
        for up in files:
            name = Path(up.filename or "").name
            ext = Path(name).suffix.lower()
            if ext in ZIP_EXT:
                tmp_zip = _staging_root() / token / "upload.zip"
                with tmp_zip.open("wb") as fh:
                    while chunk := await up.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_UPLOAD_BYTES:
                            raise HTTPException(413, "dépôt trop volumineux (max 4 Go)")
                        fh.write(chunk)
                total += _extract_zip(tmp_zip, files_dir, warnings)
                tmp_zip.unlink(missing_ok=True)
            elif ext in AUDIO_EXT | IMAGE_EXT:
                dest = _unique_path(files_dir, _sanitize_filename(name) or f"file{ext}")
                with dest.open("wb") as fh:
                    while chunk := await up.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_UPLOAD_BYTES:
                            raise HTTPException(413, "dépôt trop volumineux (max 4 Go)")
                        fh.write(chunk)
            else:
                warnings.append(f"{name} : format ignoré")
            if len(list(files_dir.iterdir())) > MAX_FILES:
                raise HTTPException(413, f"trop de fichiers (max {MAX_FILES})")
    except Exception:
        shutil.rmtree(_staging_root() / token, ignore_errors=True)
        raise

    return _analyze_files_dir(token, files_dir, warnings)


@router.post("/analyze-staged")
def analyze_staged(payload: StagedIn,
                   identity: dict = Depends(require_gestionnaire)) -> dict:
    """Analyse un staging déjà rempli par l'upload chunké (gros MP3/MP4).

    Symétrique de `/analyze` (dépôt multipart) mais sans corps : les fichiers
    sont déjà sur le volume. Sert l'import de concerts vidéo trop lourds pour un
    envoi en une requête."""
    staging = _staging(payload.token)
    return _analyze_files_dir(payload.token, staging / "files", [])


def _analyze_files_dir(token: str, files_dir: Path, warnings: list[str]) -> dict:
    """Lit les fichiers d'un staging et propose un pré-remplissage du formulaire.

    Accepte audio (MP3) comme vidéo (MP4…) : les pistes vidéo pré-découpées sont
    traitées comme des pistes à part entière ; un fichier vidéo unique sera
    découpé plus tard par l'IA (le front pilote ce choix)."""
    media = sorted([p for p in files_dir.iterdir()
                    if p.suffix.lower() in MEDIA_EXT], key=lambda p: p.name)
    if not media:
        shutil.rmtree(_staging_root() / token, ignore_errors=True)
        raise HTTPException(400, "aucun fichier audio ou vidéo trouvé dans le dépôt")

    tracks = [_read_media(p) for p in media]
    has_video = any(t.get("media") == "video" for t in tracks)

    # Numérotation : on garde celle des tags/noms de fichiers si elle est
    # complète et sans doublon, sinon on renumérote dans l'ordre alphabétique.
    ns = [t["n"] for t in tracks]
    if any(n is None for n in ns) or len(set(ns)) != len(ns):
        if any(n is not None for n in ns):
            warnings.append("Numéros de piste incomplets ou en double — renumérotation automatique")
        for i, t in enumerate(tracks, start=1):
            t["n"] = i
    tracks.sort(key=lambda t: t["n"])

    # Pochette : image fournie (indice « cover » prioritaire) sinon APIC embarqué.
    images = [p.name for p in files_dir.iterdir() if p.suffix.lower() in IMAGE_EXT]
    traycard = next((n for n in sorted(images) if _TRAYCARD_HINT_RE.search(n)), "")
    cover_candidates = [n for n in sorted(images) if n != traycard]
    cover = next((n for n in cover_candidates if _COVER_HINT_RE.search(n)), "")
    if not cover and cover_candidates:
        cover = cover_candidates[0]
    cover_source = "file" if cover else ""
    if not cover:
        embedded = _extract_staging_cover(files_dir, tracks)
        if embedded:
            cover, cover_source = embedded, "embedded"
    if not cover:
        warnings.append("Aucune pochette trouvée — elle pourra être ajoutée après l'import")

    artist = _most_common([t["albumartist"] for t in tracks]) \
        or _most_common([t["artist"] for t in tracks])
    album_title = _most_common([t["album"] for t in tracks])
    date = _most_common([t["date"] for t in tracks])

    if not artist or not album_title:
        warnings.append("Artiste ou titre d'album absent des métadonnées — à compléter")

    slug = project_slug({"album": {"artist": artist, "date": date}}) if artist else ""
    exists = bool(slug) and (PROJECTS_DIR / slug / "manifest.yaml").exists()
    if exists:
        warnings.append(f"Un album existe déjà pour ce slug ({slug}) — changez la date ou l'artiste")

    return {
        "token": token,
        "album": {"artist": artist, "title": album_title, "date": date,
                  "venue": "", "festival": ""},
        "tracks": [{"n": t["n"], "title": t["title"], "file": t["file"],
                    "duration": round(t["duration"], 1),
                    "media": t.get("media", "audio")} for t in tracks],
        "has_video": has_video,
        "cover": cover, "cover_source": cover_source, "traycard": traycard,
        "images": sorted(images),
        "slug": slug, "slug_exists": exists,
        "warnings": warnings,
    }


def _extract_zip(zip_path: Path, files_dir: Path, warnings: list[str]) -> int:
    written = 0
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise HTTPException(400, "archive ZIP illisible")
    with zf:
        for member in zf.infolist():
            safe = _safe_member_name(member.filename)
            if safe is None:
                continue
            dest = _unique_path(files_dir, safe)
            with zf.open(member) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            written += dest.stat().st_size
        if not written:
            warnings.append("ZIP sans MP3 ni image exploitable")
    return written


@router.get("/{token}/file/{name}")
def staging_file(token: str, name: str,
                 identity: dict = Depends(require_gestionnaire)) -> FileResponse:
    """Sert une image du staging, pour l'aperçu de pochette avant validation."""
    files_dir = _staging(token) / "files"
    # `name` vient de l'URL : on ne garde que le basename et on exige que le
    # fichier résolu soit bien dans le staging.
    path = (files_dir / Path(name).name).resolve()
    if not path.is_file() or files_dir.resolve() not in path.parents:
        raise HTTPException(404, "fichier introuvable")
    if path.suffix.lower() not in IMAGE_EXT:
        raise HTTPException(403, "aperçu limité aux images")
    return FileResponse(path)


@router.post("/commit")
def commit(payload: CommitIn,
           identity: dict = Depends(require_gestionnaire)) -> dict:
    """Crée le projet définitif depuis un staging validé."""
    staging = _staging(payload.token)
    files_dir = staging / "files"
    if not payload.artist.strip() or not payload.title.strip():
        raise HTTPException(400, "artiste et titre requis")
    if not payload.tracks:
        raise HTTPException(400, "au moins une piste requise")

    album = {"artist": payload.artist.strip(), "title": payload.title.strip(),
             "date": payload.date.strip(), "venue": payload.venue.strip(),
             "festival": payload.festival.strip()}
    slug = project_slug({"album": album})
    project_dir = PROJECTS_DIR / slug
    if (project_dir / "manifest.yaml").exists():
        raise HTTPException(409, f"un album existe déjà sous ce slug ({slug})")

    audio_dir = project_dir / "build" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Les MP3 prennent directement leur nom final : le manifest, les noms
        # de fichiers et les tags sont cohérents dès l'écriture.
        for i, t in enumerate(payload.tracks, start=1):
            src = files_dir / Path(t.file).name
            if not src.is_file():
                raise HTTPException(400, f"fichier absent du dépôt : {t.file}")
            safe = _sanitize_filename(t.title) or f"Track {i}"
            shutil.move(str(src), str(audio_dir / f"{i:02d}. {safe}.mp3"))

        m = new_manifest(
            album,
            [{"n": i, "title": t.title, "locked": True}
             for i, t in enumerate(payload.tracks, start=1)],
            target=payload.target if payload.target in ("audio_cd", "data_disc") else "audio_cd",
            source_url=payload.source_url,
        )
        m.data["source"]["label"] = payload.source_label
        m.data["published"] = bool(payload.published)
        # Les pistes arrivent déjà découpées : la pipeline n'a rien à refaire.
        for stage in m.data.get("pipeline_state", {}):
            m.data["pipeline_state"][stage] = "complete"
        m.data["meta"] = {
            "imported_by": identity.get("username") or "",
            "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "import_source": "upload",
        }
        if payload.published:
            # Publié dès l'import : c'est sa date de mise en ligne originelle.
            m.data["meta"]["first_published_at"] = m.data["meta"]["imported_at"]
        m.save(project_dir / "manifest.yaml")

        tagged = _write_album_tags(slug, m)
        _write_track_tags(slug, m)

        # La pochette devient une proposition créditée à l'importateur, comme
        # si elle avait été déposée via l'UI : c'est `_on_covers_changed` qui
        # repointe `album.cover` du manifest vers la gagnante.
        cover_id = _register_cover(slug, files_dir, payload, identity)
    except Exception:
        shutil.rmtree(project_dir, ignore_errors=True)
        raise

    embedded = 0
    if cover_id is not None:
        _on_covers_changed(slug)
        # Relecture : `_on_covers_changed` a réécrit album.cover sur disque.
        embedded = _write_album_cover(
            slug, Manifest.load(project_dir / "manifest.yaml")
        )

    shutil.rmtree(staging, ignore_errors=True)
    if payload.published:
        # Symlink Jellyfin dès maintenant si l'import est publié d'entrée
        # (au lieu du prochain passage de sync-media.sh, jusqu'à 10 min).
        jellyfin.trigger_sync()
    return {"ok": True, "slug": slug, "tracks": len(payload.tracks),
            "mp3_tagged": tagged, "cover_id": cover_id,
            "cover_embedded": embedded, "published": bool(payload.published)}


def _register_cover(slug: str, files_dir: Path, payload: "CommitIn",
                    identity: dict) -> int | None:
    """Enregistre la pochette (+ tray card) du dépôt dans la collection de l'album."""
    if not payload.cover:
        return None
    csrc = files_dir / Path(payload.cover).name
    cext = csrc.suffix.lower()
    if not csrc.is_file() or cext not in COVER_EXTS.values():
        return None

    tsrc, text = None, ""
    if payload.traycard:
        cand = files_dir / Path(payload.traycard).name
        if cand.is_file() and cand.suffix.lower() in TRAYCARD_EXTS.values():
            tsrc, text = cand, cand.suffix.lower()

    now = _now()
    username = identity.get("username") or ""
    # Clé de fichier unique sur le volume partagé prod+preprod, jamais dérivée
    # de l'`id` local — même règle que l'upload de pochette.
    key = uuid4().hex
    with get_conn() as conn:
        _ensure_profile(conn, username)
        cur = conn.execute(
            "INSERT INTO covers(slug, username, file_key, cover_ext, traycard_ext, "
            "caption, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (slug, username, key, cext, text, "", now, now),
        )
        cover_id = cur.lastrowid
        # Déplacement dans la transaction : un échec disque annule la ligne.
        covers_dir(slug).mkdir(parents=True, exist_ok=True)
        shutil.move(str(csrc), str(cover_file(slug, key, cext)))
        if tsrc is not None:
            shutil.move(str(tsrc), str(traycard_file(slug, key, text)))
    return cover_id


@router.post("/cancel")
def cancel(payload: dict, identity: dict = Depends(require_gestionnaire)) -> dict:
    """Abandonne un import et purge son staging."""
    shutil.rmtree(_staging(str(payload.get("token", ""))), ignore_errors=True)
    return {"ok": True}
