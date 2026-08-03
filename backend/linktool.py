"""Création d'album depuis un lien — analyse de la source vidéo.

Parcours en deux temps, symétrique de l'import de fichiers (`import_album`) :
1. `POST /api/tool/analyze` : yt-dlp sonde le lien (sans télécharger), puis
   DeepSeek extrait artiste/titre/date/lieu/setlist depuis le titre, la
   description et les chapitres. Fallback heuristique sans LLM (chapitres).
2. Le front pré-remplit le formulaire ; la création du projet et le
   téléchargement restent des actions explicites de l'utilisateur
   (point d'arrêt D : on ne télécharge jamais à la sonde).

Contient aussi les helpers de la phase de préparation : preview MP3 pour
l'éditeur de coupes et enregistrement de la miniature comme pochette créditée.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import llm, setlistfm, suggest
from .auth import require_gestionnaire
from .covers import _now, _on_covers_changed, cover_file, covers_dir
from .db import get_conn
from .manifest import Manifest
from .social import _ensure_profile

router = APIRouter(prefix="/api/tool", tags=["tool"])

PROBE_TIMEOUT = 90


class ProbeError(RuntimeError):
    """Le lien n'a pas pu être lu par yt-dlp."""


# --- Sonde yt-dlp ----------------------------------------------------------
def probe_url(url: str) -> dict:
    """Métadonnées de la vidéo sans téléchargement.

    `--no-playlist` : un lien YouTube copié depuis une lecture embarque souvent
    `&list=…` (radio/mix) — on ne veut que la vidéo pointée.
    """
    cmd = ["yt-dlp", "--dump-single-json", "--no-playlist", "--skip-download",
           "--no-warnings"]
    cookies = os.environ.get("YTDLP_COOKIES")
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise ProbeError("délai dépassé en lisant le lien") from e
    if proc.returncode != 0:
        lines = [l for l in (proc.stderr or "").splitlines() if l.strip()]
        raise ProbeError(lines[-1] if lines else "échec yt-dlp")
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise ProbeError("réponse yt-dlp illisible") from e
    return normalize_info(info, url)


def normalize_info(info: dict, url: str) -> dict:
    chapters = [{
        "title": (c.get("title") or "").strip(),
        "start": float(c.get("start_time") or 0.0),
        "end": float(c.get("end_time") or 0.0),
    } for c in (info.get("chapters") or []) if c.get("title")]
    raw_date = str(info.get("upload_date") or "")
    upload_date = (f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                   if len(raw_date) == 8 else "")
    return {
        "id": info.get("id") or "",
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "upload_date": upload_date,
        "duration": float(info.get("duration") or 0.0),
        "description": (info.get("description") or "")[:8000],
        "chapters": chapters,
        "thumbnail": info.get("thumbnail") or "",
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key") or "",
    }


# --- Suggestion sans LLM ---------------------------------------------------
_CHAPTER_NOISE = re.compile(r"^\s*(?:\d{1,2}[\.\)\-:]\s*|\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*[-–—]?\s*)")
_CHANNEL_NOISE = re.compile(r"\s*(?:-\s*Topic|VEVO|Official)\s*$", re.IGNORECASE)


def clean_chapter_title(title: str) -> str:
    return _CHAPTER_NOISE.sub("", title).strip() or title.strip()


def heuristic_suggestion(video: dict) -> dict:
    """Pré-remplissage sans IA : chapitres → setlist, chaîne → artiste."""
    title = video.get("title", "")
    artist = ""
    if " - " in title:
        artist = title.split(" - ", 1)[0].strip()
    if not artist:
        artist = _CHANNEL_NOISE.sub("", video.get("channel", "")).strip()
    tracks = [{
        "n": i,
        "title": clean_chapter_title(c["title"]),
        "artist": None,
        "start": c["start"],
        "end": c["end"] or None,
    } for i, c in enumerate(video.get("chapters", []), start=1)]
    return {
        "artist": artist,
        "title": title,
        "date": video.get("upload_date") or None,
        "venue": "",
        "city": "",
        "festival": "",
        "tracks": tracks,
    }


# --- Route -----------------------------------------------------------------
class AnalyzeIn(BaseModel):
    url: str


@router.post("/analyze")
def analyze(payload: AnalyzeIn,
            identity: dict = Depends(require_gestionnaire)) -> dict:
    url = (payload.url or "").strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "URL invalide (http/https attendu)")
    try:
        video = probe_url(url)
    except ProbeError as e:
        raise HTTPException(422, f"lien illisible : {e}")

    suggestion, ai = None, False
    if os.environ.get("DEEPSEEK_API_KEY"):
        try:
            suggestion = llm.extract_album_info(video)
            ai = True
        except Exception:
            suggestion = None
    if suggestion is None:
        suggestion = heuristic_suggestion(video)

    setlist_src = apply_setlistfm(suggestion)
    _resolve_suggested_artist(suggestion)
    return {"video": video, "suggestion": suggestion, "ai": ai,
            "setlist_source": setlist_src}


def _resolve_suggested_artist(suggestion: dict) -> None:
    """Canonicalise l'artiste suggéré sur Deezer quand la correspondance est
    exacte, pour que le sélecteur du formulaire s'ouvre déjà renseigné.

    Best-effort et volontairement strict : sans correspondance exacte on
    laisse le nom brut, le gestionnaire choisira lui-même dans la liste. Un
    rapprochement approximatif rattacherait l'album au mauvais artiste.
    """
    name = (suggestion.get("artist") or "").strip()
    if not name:
        return
    try:
        results = suggest.search_artists(name)
    except Exception:
        return
    for r in results:
        if r["label"].casefold() == name.casefold():
            suggestion["artist_id"] = r["id"]
            suggestion["artist"] = r["label"]
            return


def apply_setlistfm(suggestion: dict) -> dict | None:
    """Complète la suggestion avec la setlist officielle du concert.

    L'IA lit le titre et la description de la vidéo — souvent incomplets ;
    setlist.fm donne les titres exacts, leur ordre et les invités. On ne
    remplace la setlist que si l'on n'y perd pas d'information : jamais
    lorsque des timecodes ont déjà été trouvés (chapitres de la vidéo) et que
    le nombre de titres diffère.
    """
    artist, date = suggestion.get("artist", ""), suggestion.get("date") or ""
    try:
        sl = setlistfm.lookup(artist, date)
    except setlistfm.SetlistUnavailable:
        return None
    if not sl or not sl["tracks"]:
        return None

    current = suggestion.get("tracks") or []
    has_timecodes = any(t.get("start") is not None for t in current)
    if has_timecodes and len(current) != len(sl["tracks"]):
        return None                       # les timecodes priment
    if has_timecodes:
        # Mêmes morceaux : on garde les timecodes, on prend les titres officiels.
        for t, off in zip(current, sl["tracks"]):
            t["title"] = off["title"]
            if off["artist"]:
                t["artist"] = off["artist"]
    else:
        suggestion["tracks"] = sl["tracks"]
    # Lieu : la donnée officielle est plus fiable que celle déduite du titre.
    if sl["venue"]:
        suggestion["venue"] = sl["venue"]
    if sl["city"] and not suggestion.get("city"):
        suggestion["city"] = sl["city"]
    if sl["tour"] and not suggestion.get("festival"):
        suggestion["festival"] = sl["tour"]
    # Obligation d'attribution : l'URL suit la donnée jusqu'à l'affichage.
    suggestion["setlistfm_url"] = sl["url"]
    return {"name": setlistfm.ATTRIBUTION, "url": sl["url"],
            "tracks": len(sl["tracks"])}


# --- Helpers phase préparation --------------------------------------------
def make_preview(project_dir: Path, manifest: Manifest) -> Path:
    """MP3 léger servi à l'éditeur de coupes (le WAV master est trop lourd)."""
    wav = project_dir / manifest.data["source"]["master_wav"]
    out = project_dir / "source" / "preview.mp3"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-i", str(wav),
        "-c:a", "libmp3lame", "-b:a", "128k",
        str(out),
    ], check=True, capture_output=True)
    return out


def register_thumbnail_cover(slug: str, thumb_url: str, username: str) -> int | None:
    """Miniature de la vidéo → pochette créditée au créateur du projet.

    Même mécanique que l'import de fichiers : ligne `covers` + fichier sous
    `artwork/covers/`, puis `_on_covers_changed` repointe `album.cover` du
    manifest vers la gagnante. Ignore silencieusement si une pochette existe
    déjà pour cet album (re-préparation).
    """
    if not thumb_url:
        return None
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM covers WHERE slug=?",
                           (slug,)).fetchone()
        if row["c"]:
            return None

    r = requests.get(thumb_url, timeout=20)
    r.raise_for_status()
    ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
    ext = {"image/jpeg": ".jpg", "image/png": ".png"}.get(ctype)
    if ext is None:
        # yt-dlp donne souvent un .webp : conversion JPEG pour rester dans
        # les formats embarquables partout (APIC, artwork PDF).
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as fh:
            fh.write(r.content)
            tmp_in = fh.name
        tmp_out = tmp_in + ".jpg"
        try:
            subprocess.run(["ffmpeg", "-y", "-i", tmp_in, tmp_out],
                           check=True, capture_output=True)
            data = Path(tmp_out).read_bytes()
        finally:
            for p in (tmp_in, tmp_out):
                Path(p).unlink(missing_ok=True)
        ext = ".jpg"
    else:
        data = r.content

    now = _now()
    key = uuid4().hex
    with get_conn() as conn:
        _ensure_profile(conn, username)
        cur = conn.execute(
            "INSERT INTO covers(slug, username, file_key, cover_ext, traycard_ext, "
            "caption, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (slug, username, key, ext, "", "", now, now),
        )
        cover_id = cur.lastrowid
        covers_dir(slug).mkdir(parents=True, exist_ok=True)
        cover_file(slug, key, ext).write_bytes(data)
    _on_covers_changed(slug)
    return cover_id


def delete_project_social(slug: str) -> None:
    """Purge les lignes sociales d'un projet supprimé (brouillon abandonné)."""
    with get_conn() as conn:
        ids = [r["id"] for r in
               conn.execute("SELECT id FROM covers WHERE slug=?", (slug,))]
        if ids:
            marks = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM cover_likes WHERE cover_id IN ({marks})", ids)
            conn.execute(f"DELETE FROM comments WHERE cover_id IN ({marks})", ids)
            conn.execute("DELETE FROM covers WHERE slug=?", (slug,))
