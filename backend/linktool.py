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


# --- Résolveur mytaratata --------------------------------------------------
# yt-dlp ne connaît pas mytaratata.com (« Unsupported URL »), mais chaque page
# embarque un MP4 direct (JWPlayer) parfaitement lisible. On résout donc la
# page vers ce MP4 + son titre/pochette avant de passer la main à yt-dlp.
_MYTARATATA_PAGE = re.compile(r"^https?://(?:www\.)?mytaratata\.com/", re.I)
_TARA_SOURCE = re.compile(r'data-source="([^"]+\.mp4)"', re.I)
_TARA_H1 = re.compile(r"<h1>([^<]+)</h1>", re.I)
_TARA_IMG = re.compile(r'data-image="([^"]+)"', re.I)


def resolve_mytaratata(url: str) -> dict:
    """Page mytaratata -> info normalisée pointant le MP4 embarqué.

    Le titre de la page (« Artiste "Chanson" (année) ») est plus fiable que le
    nom de fichier du MP4 ; il est conservé tel quel et interprété plus tard
    (split_song). Le MP4 devient l'URL de téléchargement (yt-dlp la lit en
    extracteur générique).
    """
    import html
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except requests.RequestException as e:
        raise ProbeError(f"page mytaratata illisible : {e}") from e
    page = r.text
    msrc = _TARA_SOURCE.search(page)
    if not msrc:
        raise ProbeError("aucune vidéo trouvée sur la page mytaratata")
    mp4 = msrc.group(1)
    h1 = _TARA_H1.search(page)
    title = html.unescape(h1.group(1)).strip() if h1 else ""
    img = _TARA_IMG.search(page)
    return {
        "id": mp4.rsplit("/", 1)[-1].rsplit(".", 1)[0],
        "title": title,
        "channel": "Taratata",
        "upload_date": "",
        "duration": 0.0,          # inconnue ici ; mesurée au téléchargement
        "description": "",
        "chapters": [],
        "thumbnail": img.group(1) if img else "",
        "webpage_url": mp4,       # cible de téléchargement (MP4 direct)
        "extractor": "mytaratata",
    }


# --- Sonde yt-dlp ----------------------------------------------------------
def probe_url(url: str) -> dict:
    """Métadonnées de la vidéo sans téléchargement.

    `--no-playlist` : un lien YouTube copié depuis une lecture embarque souvent
    `&list=…` (radio/mix) — on ne veut que la vidéo pointée.
    """
    # mytaratata : résolution maison vers le MP4 embarqué (yt-dlp ne gère pas
    # les pages du site, seulement le fichier MP4 final).
    if _MYTARATATA_PAGE.match(url) and "videos.mytaratata.com" not in url:
        return resolve_mytaratata(url)
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


# --- Découpe artiste / titre pour un clip mono-chanson ---------------------
_SHOW_TAIL = re.compile(r"\s*/\s*TARATATA.*$", re.IGNORECASE)
# Année finale, éventuellement entre parenthèses (mytaratata : « … (2024) »).
_TRAIL_YEAR = re.compile(r"\s*\(?\b(?:19|20)\d{2}\b\)?\s*$")
_QUOTES = re.compile(r"[«»\"“”]")


def split_song(video: dict) -> tuple[str, str]:
    """Sépare « Artiste - Titre » d'une vidéo pointant une seule chanson.

    Les captations mytaratata (et la plupart des extraits live) titrent
    ``Artiste - Chanson / TARATATA … AAAA``. On isole l'artiste (avant le
    premier « - »), on retire le suffixe d'émission et l'année ; le reste
    (medley « A / B / C » compris) devient le titre. Best-effort : le
    gestionnaire relit et corrige chaque ligne dans le formulaire.
    """
    title = (video.get("title") or "").strip()
    artist, song = "", title
    # Format mytaratata : « Artiste "Chanson" (Reprise) (année) ». On prend
    # l'artiste avant le 1er guillemet, puis TOUT le reste (on ne garde pas
    # que la partie citée : « (The Animals) » est un crédit de reprise à
    # conserver). Les guillemets sont retirés, l'année finale tombe plus bas.
    mt = re.match(r'^(.*?)\s*[«"“”](.+)$', title)
    if mt:
        artist = mt.group(1).strip()
        song = _QUOTES.sub("", mt.group(2)).strip()
    elif " - " in title:
        artist, song = (p.strip() for p in title.split(" - ", 1))
    if not artist:
        artist = _CHANNEL_NOISE.sub("", video.get("channel", "")).strip()
    song = _SHOW_TAIL.sub("", song)
    song = _TRAIL_YEAR.sub("", song).strip()
    return artist, song or title


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

    setlist_src = apply_setlistfm(suggestion, video)
    _resolve_suggested_artist(suggestion)
    return {"video": video, "suggestion": suggestion, "ai": ai,
            "setlist_source": setlist_src}


class AnalyzeMultiIn(BaseModel):
    urls: list[str]


@router.post("/analyze-multi")
def analyze_multi(payload: AnalyzeMultiIn,
                  identity: dict = Depends(require_gestionnaire)) -> dict:
    """Sonde plusieurs liens, chacun devenant une piste d'un album unique.

    Cas d'usage : regrouper les passages d'un même artiste (plusieurs vidéos
    « une chanson » — cf. mytaratata) en un seul album. Chaque lien est sondé
    indépendamment ; un lien illisible n'interrompt pas les autres (il revient
    avec `error`, le formulaire l'affiche pour correction). Aucun
    téléchargement ici (point d'arrêt D) : la concaténation a lieu à la
    préparation.
    """
    urls = [u.strip() for u in (payload.urls or []) if u and u.strip()]
    if not urls:
        raise HTTPException(400, "aucun lien fourni")
    if len(urls) > 30:
        raise HTTPException(400, "30 liens maximum par album")

    clips: list[dict] = []
    artist_votes: dict[str, int] = {}
    for url in urls:
        if not re.match(r"^https?://", url):
            clips.append({"url": url, "error": "URL invalide (http/https attendu)"})
            continue
        try:
            video = probe_url(url)
        except ProbeError as e:
            clips.append({"url": url, "error": f"lien illisible : {e}"})
            continue
        artist, song = split_song(video)
        if artist:
            key = artist.casefold()
            artist_votes[key] = artist_votes.get(key, 0) + 1
        clips.append({
            "url": video.get("webpage_url") or url,
            "title": song,
            "artist": "",              # rempli seulement si ≠ artiste album
            "clip_artist": artist,     # artiste déduit du clip (indicatif)
            "thumbnail": video.get("thumbnail") or "",
            "duration": video.get("duration") or 0.0,
            "channel": video.get("channel") or "",
        })

    ok = [c for c in clips if not c.get("error")]
    if not ok:
        raise HTTPException(422, "aucun lien exploitable")

    # Artiste d'album = le plus fréquent parmi les clips lisibles.
    album_artist = ""
    if artist_votes:
        top = max(artist_votes.values())
        for c in ok:
            if c["clip_artist"] and artist_votes.get(c["clip_artist"].casefold()) == top:
                album_artist = c["clip_artist"]
                break

    suggestion = {"artist": album_artist, "title": "",
                  "date": None, "venue": "", "city": "", "festival": ""}
    _resolve_suggested_artist(suggestion)
    return {"clips": clips, "suggestion": suggestion,
            "thumbnail": next((c["thumbnail"] for c in ok if c["thumbnail"]), "")}


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


# Mots qui disqualifient un candidat « artiste » issu du titre de la vidéo.
_NOT_ARTIST = re.compile(
    r"^(?:\d{4}|live|full\s*(?:show|concert|set)|concert|festival|hd|4k|"
    r"pro\s*shot|officiel|official)$", re.IGNORECASE)


def artist_candidates(suggestion: dict, video: dict | None) -> list[str]:
    """Noms d'artiste à essayer sur setlist.fm, du plus probable au moins.

    L'IA confond régulièrement le festival et l'artiste quand le titre est de
    la forme « Main Square 2026 - Twenty One Pilots » : on essaie donc aussi
    les deux moitiés du titre de la vidéo.
    """
    out = [(suggestion.get("artist") or "").strip()]
    title = (video or {}).get("title", "") or ""
    for part in re.split(r"\s+[-–—|:]\s+", title):
        part = _TRAIL_YEAR.sub("", part).strip(" -–—|:\"'")
        if part and not _NOT_ARTIST.match(part) and len(part) < 60:
            out.append(part)
    return out


def apply_setlistfm(suggestion: dict, video: dict | None = None) -> dict | None:
    """Complète la suggestion avec la setlist officielle du concert.

    L'IA lit le titre et la description de la vidéo — souvent incomplets ;
    setlist.fm donne les titres exacts, leur ordre et les invités. On ne
    remplace la setlist que si l'on n'y perd pas d'information : jamais
    lorsque des timecodes ont déjà été trouvés (chapitres de la vidéo) et que
    le nombre de titres diffère.
    """
    date = suggestion.get("date") or ""
    year = None
    if not date:
        # Pas de date : l'année suffit à cadrer la recherche (« … 2026 »).
        m = re.search(r"\b(19|20)\d{2}\b",
                      f'{(video or {}).get("title", "")} '
                      f'{suggestion.get("festival", "")} {suggestion.get("title", "")}')
        year = int(m.group(0)) if m else None
    try:
        sl = setlistfm.lookup_flexible(
            artist_candidates(suggestion, video), date,
            venue=suggestion.get("venue", ""), city=suggestion.get("city", ""),
            year=year)
    except setlistfm.SetlistUnavailable:
        return None
    if not sl or not sl["tracks"]:
        return None
    # La donnée officielle corrige l'IA : nom exact de l'artiste et date réelle
    # du concert (l'IA devine souvent une date d'édition de festival).
    if sl.get("artist"):
        suggestion["artist"] = sl["artist"]
        suggestion.pop("artist_id", None)
    if sl.get("date"):
        suggestion["date"] = sl["date"]

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
        "-c:a", "libmp3lame", "-b:a", "64k", "-ac", "1",
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
            "caption, auto, created_at, updated_at) VALUES(?,?,?,?,?,?,1,?,?)",
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
