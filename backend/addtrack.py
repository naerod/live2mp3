"""Ajout d'une piste isolée à un album existant, depuis un lien vidéo.

Complète les deux parcours de création existants, qui produisent tous deux un
album **entier** : `linktool` (un lien -> un concert découpé) et `import_album`
(des fichiers déjà découpés -> un album). Il manquait le cas de la compilation
qu'on enrichit au fil de l'eau — « Live Crossovers » agrège des featurings
issus de captations différentes, une piste à la fois.

Différence structurelle avec un album de concert : ici chaque piste a **sa
propre source**, là où un concert découpe un master unique. Cette source est
écrite dans l'entrée de piste du manifeste (`track.source`), et non dans
`source` au niveau album :

    tracks:
    - n: 6
      title: Coldplay (ft. Ed Sheeran) - Fix You
      start: null            # pas de timecode d'album : le fichier est autonome
      end: null
      source:
        url: https://www.youtube.com/watch?v=...
        start: 12.0          # rognage dans la vidéo (optionnel)
        end: 305.5
        added_by: naerod
        added_at: '2026-08-03T...'

Conséquence traitée dans `pipeline/render.py` : une piste externe n'est jamais
re-découpée depuis le master de l'album, et son fichier n'est pas considéré
comme orphelin par la purge du rendu.

Parcours en deux temps, symétrique du reste de l'outil (point d'arrêt D : on
ne télécharge jamais à la sonde) :
1. `POST /api/tool/probe-track` : yt-dlp sonde le lien, on en déduit un
   artiste et un titre. Pas de LLM ni de setlist.fm — il s'agit d'un morceau
   unique, le titre de la vidéo suffit et l'appel serait payé pour rien.
2. `POST /api/albums/{slug}/tracks/from-url` : téléchargement, rognage,
   encodage MP3, tags, pochette de piste. Exécuté en tâche de fond (yt-dlp
   dépasse volontiers la minute), suivi par `GET .../from-url/{token}`.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from mutagen.id3 import ID3
from pydantic import BaseModel

from . import jellyfin
from .albumfiles import _write_album_tags, _write_track_tags
from .auth import require_gestionnaire
from .covers import _now, track_cover_file, track_covers_dir
from .db import get_conn
from .linktool import ProbeError, probe_url
from .manifest import PROJECTS_DIR, Manifest, sanitize_filename
from .pipeline import download, preanalyze
from .social import _ensure_profile

router = APIRouter(tags=["addtrack"])

# Un ajout abandonné (onglet fermé) laisse son état en mémoire : purgé au bout
# de ce délai, comme le staging d'import.
JOB_TTL_S = 3600

# --- Suivi des ajouts en cours ---------------------------------------------
# L'ajout tourne dans un thread du process API (même mécanique que la
# préparation d'un album, cf. main.py::start_prepare) : un dictionnaire en
# mémoire suffit, il n'y a rien à partager avec le worker RQ.
_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


def _job_set(token: str, **fields: Any) -> None:
    with _JOBS_LOCK:
        job = _JOBS.setdefault(token, {"created_at": time.time()})
        job.update(fields)


def _job_get(token: str) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        job = _JOBS.get(token)
        return dict(job) if job else None


def _purge_jobs() -> None:
    cutoff = time.time() - JOB_TTL_S
    with _JOBS_LOCK:
        for token in [t for t, j in _JOBS.items() if j["created_at"] < cutoff]:
            del _JOBS[token]


# --- Sonde d'un morceau isolé ----------------------------------------------
# Bruit récurrent des titres YouTube : on le retire du titre de la piste, mais
# jamais la mention « (Live at ...) » ni « (ft. ...) », qui sont l'information.
_TITLE_NOISE = re.compile(
    r"\s*[\(\[](?:official\s*)?(?:music\s*)?(?:video|audio|lyric[s]?|hd|4k|"
    r"clip officiel|visualizer)[^)\]]*[\)\]]", re.I)


def split_artist_title(video_title: str, uploader: str) -> tuple[str, str]:
    """Sépare « Artiste - Titre » ; repli sur la chaîne pour l'artiste.

    Le premier séparateur seulement : « Shakira, Ed Sheeran, Beéle - Hips
    Don't Lie (Anniversary Version) » doit rester entier à droite comme à
    gauche.
    """
    clean = _TITLE_NOISE.sub("", video_title or "").strip()
    for sep in (" - ", " – ", " — ", " | "):
        if sep in clean:
            left, right = clean.split(sep, 1)
            return left.strip(), right.strip()
    return (uploader or "").strip(), clean


def suggest_track_title(artist: str, title: str) -> str:
    """Titre de piste au format de la compilation : « Artiste - Morceau ».

    Sur un album-compilation le nom de l'artiste ne peut pas venir du niveau
    album (il diffère à chaque piste) : la convention retenue sur
    `live-crossovers` est de le porter dans le titre de la piste.
    """
    artist, title = artist.strip(), title.strip()
    if not artist:
        return title
    if not title:
        return artist
    return f"{artist} - {title}"


class ProbeIn(BaseModel):
    url: str


@router.post("/api/tool/probe-track")
def probe_track(payload: ProbeIn,
                identity: dict = Depends(require_gestionnaire)) -> dict:
    url = (payload.url or "").strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "URL invalide (http/https attendu)")
    try:
        video = probe_url(url)
    except ProbeError as e:
        raise HTTPException(422, f"lien illisible : {e}")
    artist, title = split_artist_title(video.get("title", ""),
                                       video.get("channel", ""))
    return {
        "video": video,
        "suggestion": {
            "artist": artist,
            "title": title,
            "track_title": suggest_track_title(artist, title),
        },
    }


# --- Ajout de la piste -----------------------------------------------------
class AddTrackIn(BaseModel):
    url: str
    title: str
    artist: str | None = None
    start: float | None = None
    end: float | None = None
    thumbnail_url: str | None = None
    cover_from_thumbnail: bool = True
    # Jeton d'une découpe précise déjà préparée (cf. prep-clip) : sa source
    # audio est réutilisée telle quelle, on ne re-télécharge pas.
    prep_token: str | None = None


def _album_manifest(slug: str) -> tuple[Path, Manifest]:
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        raise HTTPException(404, "album introuvable")
    return path, Manifest.load(path)


def _work_dir(token: str) -> Path:
    """Espace de travail sous `projects/` — même volume que la destination,
    donc la mise en place du MP3 est un déplacement, pas une copie."""
    return PROJECTS_DIR / ".l2m-addtrack" / token


def encode_track(src: Path, out: Path, start: float | None,
                 end: float | None) -> Path:
    """Rognage + encodage MP3 VBR — mêmes réglages que le rendu d'album
    (`libmp3lame -q:a 0`), pour que les pistes d'une compilation soient
    homogènes avec celles produites par le pipeline."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y"]
    if start:
        cmd += ["-ss", f"{float(start):.3f}"]
    cmd += ["-i", str(src)]
    if end is not None:
        duration = float(end) - float(start or 0.0)
        if duration <= 0:
            raise ValueError("fin de rognage avant le début")
        cmd += ["-t", f"{duration:.3f}"]
    # `.part` : un fichier partiel portant le nom final serait servi comme
    # piste valide (même piège que le rendu, cf. render._render_or_cleanup).
    part = out.with_name(out.name + ".part")
    part.unlink(missing_ok=True)
    cmd += ["-vn", "-c:a", "libmp3lame", "-q:a", "0", "-f", "mp3", str(part)]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        part.unlink(missing_ok=True)
        tail = (e.stderr or b"").decode("utf-8", "replace").splitlines()[-5:]
        raise RuntimeError("ffmpeg a échoué :\n" + "\n".join(tail)) from e
    os.replace(part, out)
    return out


def register_track_cover(slug: str, n: int, thumb_url: str,
                         username: str) -> int | None:
    """Miniature de la vidéo -> pochette de la piste ajoutée.

    Sur un album `per_track_covers`, c'est la seule illustration que verra
    cette piste. Best-effort : un échec de téléchargement d'image ne doit pas
    faire échouer un ajout dont l'audio est déjà en place.
    """
    if not thumb_url:
        return None
    try:
        r = requests.get(thumb_url, timeout=20)
        r.raise_for_status()
    except requests.RequestException:
        return None
    ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
    ext = {"image/jpeg": ".jpg", "image/png": ".png",
           "image/webp": ".webp"}.get(ctype, ".jpg")
    now = _now()
    key = uuid4().hex
    with get_conn() as conn:
        _ensure_profile(conn, username)
        existing = conn.execute(
            "SELECT * FROM track_covers WHERE slug=? AND track_n=?",
            (slug, n)).fetchone()
        track_covers_dir(slug).mkdir(parents=True, exist_ok=True)
        track_cover_file(slug, key, ext).write_bytes(r.content)
        if existing:
            track_cover_file(slug, existing["file_key"],
                             existing["cover_ext"]).unlink(missing_ok=True)
            conn.execute(
                "UPDATE track_covers SET file_key=?, cover_ext=?, username=?, "
                "updated_at=? WHERE slug=? AND track_n=?",
                (key, ext, username, now, slug, n))
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO track_covers(slug, track_n, username, file_key, "
            "cover_ext, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (slug, n, username, key, ext, now, now))
        return cur.lastrowid


def _rollback_track(slug: str, n: int) -> None:
    """Retire du manifeste la piste dont la production a échoué.

    Sans ça l'album garde une piste fantôme, sans fichier : elle apparaîtrait
    dans la vitrine et décalerait la numérotation ID3 de toutes les suivantes.
    """
    try:
        m = Manifest.load(PROJECTS_DIR / slug / "manifest.yaml")
        m.data["tracks"] = [t for t in m.data.get("tracks", [])
                            if int(t.get("n", -1)) != n]
        m.save()
    except Exception:
        pass


def _run_add_bg(token: str, slug: str, n: int, payload: AddTrackIn,
                username: str) -> None:
    work = _work_dir(token)
    prep = _prep_source(payload.prep_token) if payload.prep_token else None
    try:
        if prep is not None:
            # La source a déjà été téléchargée pour la découpe précise : on la
            # réutilise telle quelle, pas de second passage yt-dlp.
            _job_set(token, state="running", stage="encode", pct=0.0)
            src = prep
        else:
            _job_set(token, state="running", stage="download", pct=0.0)
            cookies = os.environ.get("YTDLP_COOKIES")
            src = download.download_audio(
                payload.url, work, cookies,
                progress=lambda pct: _job_set(token, stage="download", pct=pct))
            _job_set(token, stage="encode", pct=0.0)
        # Le manifeste est relu ici : entre la création de l'entrée et la fin
        # du téléchargement, le gestionnaire a pu renommer la piste depuis la
        # page de gestion — le nom de fichier doit suivre le manifeste.
        m = Manifest.load(PROJECTS_DIR / slug / "manifest.yaml")
        track = next((t for t in m.tracks if int(t.get("n", -1)) == n), None)
        if track is None:
            raise RuntimeError("piste retirée du manifeste pendant l'ajout")
        out = (PROJECTS_DIR / slug / "build" / "audio"
               / m.track_filename(track, "mp3"))
        encode_track(src, out, payload.start, payload.end)

        _job_set(token, stage="tags", pct=0.0)
        _write_track_tags(slug, m)
        _write_album_tags(slug, m)

        cover_id = None
        if payload.cover_from_thumbnail:
            cover_id = register_track_cover(slug, n,
                                            payload.thumbnail_url or "",
                                            username)

        # Jellyfin ne détecte pas seul l'ajout d'un fichier dans un album déjà
        # monté (même raison qu'après un re-rendu, cf. jellyfin.py).
        try:
            jellyfin.refresh_album(slug)
        except Exception:
            pass

        _job_set(token, state="done", stage="done", pct=100.0,
                 n=n, file=out.name, track_cover_id=cover_id)
    except Exception as e:
        _rollback_track(slug, n)
        _job_set(token, state="error", error=str(e) or e.__class__.__name__)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if payload.prep_token:
            shutil.rmtree(_prep_dir(payload.prep_token), ignore_errors=True)


# --- Cohabitation avec l'éditeur de coupes ---------------------------------
def _remap_track_covers(slug: str, remap: dict[int, int]) -> None:
    """Suit les pochettes de piste quand une piste externe change de numéro.

    En deux passes par des numéros négatifs : `track_covers` porte un index
    unique sur (slug, track_n), et une renumérotation vers le haut croiserait
    sinon une ligne encore en place.
    """
    if not remap:
        return
    with get_conn() as conn:
        for old in remap:
            conn.execute(
                "UPDATE track_covers SET track_n=? WHERE slug=? AND track_n=?",
                (-old, slug, old))
        for old, new in remap.items():
            conn.execute(
                "UPDATE track_covers SET track_n=? WHERE slug=? AND track_n=?",
                (new, slug, -old))


def preserve_external_tracks(slug: str, m: Manifest,
                             new_tracks: list[dict]) -> list[dict]:
    """Replace les pistes externes derrière une setlist réécrite.

    `PUT /api/jobs/{slug}/setlist` remplace la liste **entière** par ce que
    l'éditeur de coupes a produit — or l'éditeur ne connaît que le découpage
    du master, donc pas les pistes ajoutées depuis un lien. Sans ce recollage,
    valider les coupes d'un album hybride effacerait ces pistes du manifeste
    (leurs MP3 seraient ensuite purgés comme orphelins).
    """
    externals = [t for t in m.data.get("tracks", []) if t.get("source")]
    if not externals:
        return new_tracks
    remap: dict[int, int] = {}
    for i, track in enumerate(externals, start=len(new_tracks) + 1):
        old = int(track.get("n", -1))
        track = dict(track)
        track["n"] = i
        if old != i:
            remap[old] = i
        new_tracks.append(track)
    _remap_track_covers(slug, remap)
    return new_tracks


@router.post("/api/albums/{slug}/tracks/from-url")
def add_track_from_url(slug: str, payload: AddTrackIn,
                       identity: dict = Depends(require_gestionnaire)) -> dict:
    """Ajoute une piste en fin de tracklist et lance sa production."""
    url = (payload.url or "").strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "URL invalide (http/https attendu)")
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(400, "titre de piste requis")
    if not sanitize_filename(title):
        raise HTTPException(400, "titre de piste sans caractère exploitable")
    if payload.end is not None and payload.end <= (payload.start or 0.0):
        raise HTTPException(400, "rognage : fin avant début")

    _, m = _album_manifest(slug)
    tracks = m.data.setdefault("tracks", [])
    n = max((int(t.get("n", 0)) for t in tracks), default=0) + 1
    entry: dict[str, Any] = {
        "n": n, "title": title, "start": None, "end": None, "locked": False,
        "source": {
            "url": url,
            "added_by": identity.get("username") or "",
            "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    }
    if (payload.artist or "").strip():
        entry["artist"] = payload.artist.strip()
    if payload.start:
        entry["source"]["start"] = float(payload.start)
    if payload.end is not None:
        entry["source"]["end"] = float(payload.end)
    tracks.append(entry)
    m.save()

    _purge_jobs()
    token = uuid4().hex
    _job_set(token, state="queued", stage="download", pct=0.0, slug=slug, n=n)
    threading.Thread(target=_run_add_bg,
                     args=(token, slug, n, payload,
                           identity.get("username") or ""),
                     daemon=True).start()
    return {"ok": True, "token": token, "n": n, "title": title}


# --- Découpe précise à la waveform -----------------------------------------
# La saisie « minute:seconde » du panneau d'ajout ne permet pas de caler un
# début pile sur l'attaque d'un morceau (parlote d'intro, applaudissements).
# `prep-clip` télécharge la source **une seule fois**, en produit une waveform
# (réutilise `generate_waveform`, comme l'éditeur d'album) et un MP3 léger de
# lecture, que le front affiche avec deux poignées début/fin. À la validation,
# `from-url` réutilise cette même source (jeton `prep_token`) : pas de second
# téléchargement, et le rognage garde la précision au millième de seconde déjà
# supportée par `encode_track`.
def _prep_dir(token: str) -> Path:
    return PROJECTS_DIR / ".l2m-addtrack" / f"prep-{token}"


def _prep_source(token: str) -> Path | None:
    """Fichier source téléchargé d'une préparation, s'il est encore là."""
    d = _prep_dir(token)
    files = sorted(d.glob("master_audio.*"))
    return files[0] if files else None


def _sweep_prep_dirs() -> None:
    """Efface les préparations abandonnées (onglet fermé) au-delà du TTL.

    Une préparation retient une source audio sur disque tant qu'elle n'a pas
    été consommée par un ajout : sans ce balayage, un aller-retour interrompu
    laisserait le fichier indéfiniment."""
    root = PROJECTS_DIR / ".l2m-addtrack"
    if not root.exists():
        return
    cutoff = time.time() - JOB_TTL_S
    for d in root.glob("prep-*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _run_prep_bg(token: str, url: str) -> None:
    work = _prep_dir(token)
    try:
        _job_set(token, state="running", stage="download", pct=0.0)
        cookies = os.environ.get("YTDLP_COOKIES")
        src = download.download_audio(
            url, work, cookies,
            progress=lambda pct: _job_set(token, stage="download", pct=pct))

        _job_set(token, stage="waveform", pct=0.0)
        wav = download.extract_wav(src, work / "clip.wav")
        preanalyze.generate_waveform(wav, work / "waveform.dat")

        _job_set(token, stage="preview", pct=0.0)
        preview = work / "preview.mp3"
        subprocess.run([
            "ffmpeg", "-y", "-i", str(wav),
            "-vn", "-c:a", "libmp3lame", "-b:a", "64k", "-ac", "1", str(preview),
        ], check=True, capture_output=True)

        # La waveform (donc le lecteur) porte sur le WAV : sa durée réelle est
        # la référence des poignées, plus fiable que la durée annoncée par la
        # sonde (parfois arrondie).
        duration = _wav_seconds(wav)
        # `clip.wav` ne sert plus qu'à la génération : on le retire pour ne pas
        # laisser un PCM volumineux le temps que l'ajout consomme la source.
        wav.unlink(missing_ok=True)
        _job_set(token, state="done", stage="done", pct=100.0, duration=duration)
    except Exception as e:
        shutil.rmtree(work, ignore_errors=True)
        _job_set(token, state="error", error=str(e) or e.__class__.__name__)


def _wav_seconds(wav: Path) -> float:
    import wave
    try:
        with wave.open(str(wav), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (wave.Error, OSError):
        return 0.0


@router.post("/api/albums/{slug}/tracks/prep-clip")
def prep_clip(slug: str, payload: ProbeIn,
              identity: dict = Depends(require_gestionnaire)) -> dict:
    """Télécharge et prépare une source pour la découpe précise (waveform)."""
    _album_manifest(slug)  # 404 si l'album n'existe pas
    url = (payload.url or "").strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "URL invalide (http/https attendu)")
    _purge_jobs()
    _sweep_prep_dirs()
    token = uuid4().hex
    _job_set(token, state="queued", stage="download", pct=0.0, slug=slug)
    threading.Thread(target=_run_prep_bg, args=(token, url), daemon=True).start()
    return {"ok": True, "token": token}


@router.get("/api/albums/{slug}/tracks/prep-clip/{token}")
def prep_clip_status(slug: str, token: str,
                     identity: dict = Depends(require_gestionnaire)) -> dict:
    job = _job_get(token)
    if not job:
        raise HTTPException(404, "préparation inconnue")
    return job


@router.get("/api/albums/{slug}/tracks/prep-clip/{token}/waveform.dat")
def prep_clip_waveform(slug: str, token: str,
                       identity: dict = Depends(require_gestionnaire)):
    from fastapi.responses import FileResponse
    dat = _prep_dir(token) / "waveform.dat"
    if not dat.exists():
        raise HTTPException(404, "waveform indisponible")
    return FileResponse(str(dat), media_type="application/octet-stream")


@router.get("/api/albums/{slug}/tracks/prep-clip/{token}/audio")
def prep_clip_audio(slug: str, token: str,
                    identity: dict = Depends(require_gestionnaire)):
    from fastapi.responses import FileResponse
    mp3 = _prep_dir(token) / "preview.mp3"
    if not mp3.exists():
        raise HTTPException(404, "aperçu indisponible")
    return FileResponse(str(mp3), media_type="audio/mpeg")


# --- Dépôt de fichiers dans un album existant ------------------------------
# Pendant de `add_track_from_url` pour l'autre source de pistes : des MP3 déjà
# découpés. Symétrique de `import_album`, mais vers un album existant plutôt
# que vers un album neuf — et sans staging ni formulaire, puisqu'il n'y a
# aucune métadonnée d'album à deviner.
MAX_UPLOAD_BYTES = 512 * 1024 * 1024   # par fichier
MAX_UPLOAD_FILES = 50


def _cover_from_apic(slug: str, n: int, mp3: Path, username: str) -> int | None:
    """Pochette embarquée (APIC) du MP3 déposé -> pochette de la piste."""
    try:
        apic = ID3(str(mp3)).getall("APIC")[0]
    except Exception:
        return None
    ext = {"image/jpeg": ".jpg", "image/png": ".png",
           "image/webp": ".webp"}.get(apic.mime, ".jpg")
    now = _now()
    key = uuid4().hex
    with get_conn() as conn:
        _ensure_profile(conn, username)
        existing = conn.execute(
            "SELECT * FROM track_covers WHERE slug=? AND track_n=?",
            (slug, n)).fetchone()
        if existing:
            return existing["id"]
        track_covers_dir(slug).mkdir(parents=True, exist_ok=True)
        track_cover_file(slug, key, ext).write_bytes(apic.data)
        cur = conn.execute(
            "INSERT INTO track_covers(slug, track_n, username, file_key, "
            "cover_ext, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (slug, n, username, key, ext, now, now))
        return cur.lastrowid


@router.post("/api/albums/{slug}/tracks/upload")
async def upload_tracks(
    slug: str,
    files: list[UploadFile] = File(...),
    identity: dict = Depends(require_gestionnaire),
) -> dict:
    """Ajoute un ou plusieurs MP3 déjà découpés en fin de tracklist."""
    if not files:
        raise HTTPException(400, "aucun fichier")
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(400, f"{MAX_UPLOAD_FILES} fichiers au maximum")
    _, m = _album_manifest(slug)
    username = identity.get("username") or ""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    audio_dir = PROJECTS_DIR / slug / "build" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    staging = _work_dir(uuid4().hex)
    staging.mkdir(parents=True, exist_ok=True)
    added: list[dict] = []
    try:
        tracks = m.data.setdefault("tracks", [])
        # Les fichiers sont d'abord écrits en zone de travail : un dépôt refusé
        # en cours de route ne doit pas laisser la moitié des pistes en place.
        pending: list[tuple[Path, str]] = []
        for up in files:
            name = Path(up.filename or "").name
            if Path(name).suffix.lower() != ".mp3":
                raise HTTPException(400, f"« {name} » : seuls les MP3 sont acceptés")
            data = await up.read()
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(400, f"« {name} » : fichier trop volumineux")
            tmp = staging / f"{uuid4().hex}.mp3"
            tmp.write_bytes(data)
            pending.append((tmp, name))

        for tmp, name in pending:
            title = _title_from_upload(tmp, name)
            n = max((int(t.get("n", 0)) for t in tracks), default=0) + 1
            entry = {"n": n, "title": title, "start": None, "end": None,
                     "locked": False,
                     "source": {"file": name, "added_by": username,
                                "added_at": now}}
            tracks.append(entry)
            dest = audio_dir / m.track_filename(entry, "mp3")
            shutil.move(str(tmp), dest)
            added.append({"n": n, "title": title, "file": dest.name})
        m.save()

        _write_track_tags(slug, m)
        _write_album_tags(slug, m)
        if m.data.get("album", {}).get("per_track_covers"):
            for a in added:
                _cover_from_apic(slug, a["n"], audio_dir / a["file"], username)
        try:
            jellyfin.refresh_album(slug)
        except Exception:
            pass
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"ok": True, "added": added}


def _title_from_upload(mp3: Path, filename: str) -> str:
    """Titre de la piste : tag TIT2 s'il existe, sinon le nom du fichier nettoyé.

    Le numéro que le pipeline préfixe aux titres qu'il écrit (« 01. Titre »)
    est retiré : il sera reposé par `_write_track_tags` selon la position
    réelle dans l'album de destination, qui n'est pas celle d'origine.
    """
    title = ""
    try:
        v = ID3(str(mp3)).get("TIT2")
        title = str(v.text[0]).strip() if v and v.text else ""
    except Exception:
        title = ""
    if not title:
        title = Path(filename).stem
    title = re.sub(r"^\s*\d{1,3}\s*[-._)\s]+", "", title).replace("_", " ")
    return re.sub(r"\s+", " ", title).strip() or Path(filename).stem


@router.get("/api/albums/{slug}/tracks/from-url/{token}")
def add_track_status(slug: str, token: str,
                     identity: dict = Depends(require_gestionnaire)) -> dict:
    job = _job_get(token)
    if job is None:
        raise HTTPException(404, "ajout inconnu ou expiré")
    return {k: v for k, v in job.items() if k != "created_at"}
