"""Setlist officielle d'un concert via l'API setlist.fm.

La détection audio place correctement les frontières, mais elle ne connaît pas
les *titres*. setlist.fm les fournit, dans l'ordre, avec les invités
(« with … ») — exactement ce qui manquait pour renseigner l'artiste par piste.

Recherche autonome sur (artiste, date du concert) : l'IA extrait déjà ces deux
informations du titre et de la description de la vidéo.

⚠️ Conditions d'utilisation : toute donnée affichée doit être accompagnée d'un
lien vers la page setlist.fm correspondante. `lookup()` renvoie donc toujours
`url`, que l'appelant est tenu d'afficher (voir `meta.setlistfm_url` du
manifest et l'attribution dans l'outil / la fiche album).

Quotas de l'application : 2 requêtes/seconde, 1440/jour. Un appel par analyse
de lien, plus un cache mémoire : on reste très loin du plafond.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

import requests

BASE_URL = "https://api.setlist.fm/rest/1.0"
TIMEOUT = 8.0
CACHE_TTL = 24 * 3600      # une setlist passée ne change quasiment jamais
MIN_INTERVAL = 0.6         # garde-fou local (quota : 2 req/s)

ATTRIBUTION = "setlist.fm"

_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()
_last_call = [0.0]


class SetlistUnavailable(RuntimeError):
    """API injoignable ou clé absente — l'appelant continue sans setlist."""


def _api_key() -> str:
    return os.environ.get("SETLISTFM_API_KEY", "").strip()


def _throttle() -> None:
    with _lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def _cache_get(key: str) -> Any | None:
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        _cache.pop(key, None)
    return None


def _cache_put(key: str, value: Any) -> None:
    with _lock:
        if len(_cache) > 500:
            now = time.monotonic()
            for k in [k for k, v in _cache.items() if v[0] <= now]:
                _cache.pop(k, None)
        _cache[key] = (time.monotonic() + CACHE_TTL, value)


def to_api_date(iso_date: str) -> str | None:
    """« 2014-12-01 » -> « 01-12-2014 » (format attendu par l'API)."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", (iso_date or "").strip())
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def from_api_date(api_date: str) -> str:
    """« 05-07-2026 » -> « 2026-07-05 » (chaîne vide si illisible)."""
    m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", (api_date or "").strip())
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def _search_raw(params: dict) -> list[dict]:
    """Appel générique à /search/setlists (404 = aucun résultat, pas une erreur)."""
    key = _api_key()
    if not key:
        raise SetlistUnavailable("SETLISTFM_API_KEY absente")
    _throttle()
    r = requests.get(
        f"{BASE_URL}/search/setlists",
        params=params,
        headers={"x-api-key": key, "Accept": "application/json",
                 "User-Agent": "live2mp3 (+https://live2mp3.naerod.com)"},
        timeout=TIMEOUT,
    )
    if r.status_code == 404:       # aucun concert correspondant
        return []
    r.raise_for_status()
    return r.json().get("setlist") or []


def _search(artist: str, api_date: str) -> list[dict]:
    return _search_raw({"artistName": artist, "date": api_date})


def parse_setlist(raw: dict) -> dict:
    """Réponse API -> pistes exploitables par l'outil."""
    venue = raw.get("venue") or {}
    city = venue.get("city") or {}
    album_artist = (raw.get("artist") or {}).get("name", "")
    tracks: list[dict] = []
    for st in (raw.get("sets") or {}).get("set", []):
        for song in st.get("song") or []:
            title = (song.get("name") or "").strip()
            if not title:
                continue
            # `tape` = morceau diffusé par la sono, pas joué sur scène.
            if song.get("tape"):
                continue
            guest = ((song.get("with") or {}).get("name") or "").strip()
            tracks.append({
                "n": len(tracks) + 1,
                "title": title,
                # Même convention que le reste de l'outil : l'artiste de piste
                # n'est renseigné que s'il diffère de celui de l'album.
                "artist": f"{album_artist} with {guest}" if guest else None,
                "start": None, "end": None,
            })
    return {
        "url": raw.get("url", ""),
        "date": from_api_date(raw.get("eventDate", "")),   # « 05-07-2026 » -> ISO
        "artist": album_artist,
        "venue": venue.get("name", ""),
        "city": city.get("name", ""),
        "country": (city.get("country") or {}).get("code", ""),
        "tour": (raw.get("tour") or {}).get("name", ""),
        "tracks": tracks,
    }


def _score(parsed: dict, artist: str) -> tuple:
    """Départage plusieurs setlists le même jour.

    Une même soirée peut être publiée plusieurs fois (« U2 » et « U2 with
    Bruce Springsteen », cette dernière ne couvrant que deux titres) : on
    privilégie la plus complète, puis la correspondance exacte du nom.
    """
    exact = parsed["artist"].casefold() == (artist or "").casefold()
    return (len(parsed["tracks"]), exact)


def _fetch_by_id(setlist_id: str) -> dict:
    key = _api_key()
    if not key:
        raise SetlistUnavailable("SETLISTFM_API_KEY absente")
    _throttle()
    r = requests.get(
        f"{BASE_URL}/setlist/{setlist_id}",
        headers={"x-api-key": key, "Accept": "application/json",
                 "User-Agent": "live2mp3 (+https://live2mp3.naerod.com)"},
        timeout=TIMEOUT,
    )
    if r.status_code == 404:
        raise SetlistUnavailable("setlist introuvable")
    r.raise_for_status()
    return r.json()


def lookup_by_url(url: str) -> dict | None:
    """Setlist depuis une URL setlist.fm fournie à la main.

    Utile à l'import manuel : le gestionnaire colle le lien exact du concert,
    on n'a donc pas à deviner (artiste, date). L'id est le jeton hexadécimal en
    fin d'URL (`…-6bd6a0f1.html`)."""
    m = re.search(r"([0-9a-fA-F]{6,})\.html", url or "")
    if not m:
        return None
    setlist_id = m.group(1)
    ck = f"slid:{setlist_id}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached or None
    try:
        raw = _fetch_by_id(setlist_id)
    except SetlistUnavailable:
        raise
    except Exception as e:
        raise SetlistUnavailable(str(e)) from e
    parsed = parse_setlist(raw)
    if not parsed["tracks"]:
        _cache_put(ck, {})
        return None
    _cache_put(ck, parsed)
    return parsed


def lookup(artist: str, iso_date: str) -> dict | None:
    """Setlist officielle du concert, ou None si introuvable/indisponible."""
    artist = (artist or "").strip()
    api_date = to_api_date(iso_date)
    if not artist or not api_date:
        return None
    ck = f"sl:{artist.casefold()}:{api_date}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached or None
    try:
        raw = _search(artist, api_date)
    except SetlistUnavailable:
        raise
    except Exception as e:                       # réseau, quota, 5xx…
        raise SetlistUnavailable(str(e)) from e
    parsed = [p for p in (parse_setlist(x) for x in raw) if p["tracks"]]
    if not parsed:
        _cache_put(ck, {})
        return None
    best = max(parsed, key=lambda p: _score(p, artist))
    _cache_put(ck, best)
    return best


# --- Recherche tolérante ---------------------------------------------------
# L'IA se trompe régulièrement sur deux points : l'artiste (elle prend le nom
# du festival dans « Main Square 2026 - Twenty One Pilots ») et la date (elle
# devine une date d'édition de festival au lieu de la date du concert). La
# recherche exacte (artiste, date) échoue alors alors que la setlist existe.
# `lookup_flexible` essaie plusieurs noms d'artiste candidats et, à défaut de
# date exacte, tous les concerts de l'année, départagés par la proximité de
# date et la correspondance salle/ville.
MAX_ARTISTS = 4           # garde-fou quota (2 req/s, 1440/j)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").casefold()).strip()


def _day(iso_date: str) -> Any:
    from datetime import date
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", (iso_date or "").strip())
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _flex_score(parsed: dict, target: Any, venue: str, city: str) -> tuple:
    """Départage les concerts d'un même artiste sur une année."""
    d = _day(parsed.get("date", ""))
    delta = abs((d - target).days) if (d and target) else 999
    venue_hit = bool(venue) and _norm(venue) in _norm(parsed.get("venue", ""))
    city_hit = bool(city) and _norm(city) == _norm(parsed.get("city", ""))
    # Ordre de priorité : lieu, ville, proximité de date, richesse de la setlist.
    return (venue_hit, city_hit, -delta, len(parsed["tracks"]))


def _by_year(artist: str, year: int) -> list[dict]:
    ck = f"sly:{artist.casefold()}:{year}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    raw: list[dict] = []
    for page in (1, 2, 3):          # 20 concerts/page — 3 pages suffisent
        chunk = _search_raw({"artistName": artist, "year": year, "p": page})
        raw += chunk
        if len(chunk) < 20:
            break
    parsed = [p for p in (parse_setlist(x) for x in raw) if p["tracks"]]
    _cache_put(ck, parsed)
    return parsed


def lookup_flexible(artists: list[str], iso_date: str = "", venue: str = "",
                    city: str = "", year: int | None = None) -> dict | None:
    """Setlist du concert en tolérant une erreur de l'IA sur l'artiste/la date.

    `artists` : noms candidats, du plus probable au moins probable. Renvoie la
    meilleure correspondance, ou None. Ne lève jamais : une indisponibilité de
    l'API n'est pas une raison de bloquer l'analyse du lien.
    """
    seen: set[str] = set()
    cands = []
    for a in artists:
        a = (a or "").strip()
        if a and a.casefold() not in seen:
            seen.add(a.casefold())
            cands.append(a)
    cands = cands[:MAX_ARTISTS]
    target = _day(iso_date)
    if year is None and target:
        year = target.year

    # 1) Date exacte : le cas nominal, le moins coûteux et le plus sûr.
    for a in cands:
        try:
            hit = lookup(a, iso_date) if target else None
        except SetlistUnavailable:
            return None
        if hit:
            return hit
    if not year:
        return None

    # 2) Tous les concerts de l'artiste cette année-là, départagés au score.
    best, best_key = None, None
    for a in cands:
        try:
            found = _by_year(a, year)
        except SetlistUnavailable:
            return None
        except Exception:
            continue
        for p in found:
            d = _day(p.get("date", ""))
            exact = bool(d and target and d == target)
            venue_hit = bool(venue) and _norm(venue) in _norm(p.get("venue", ""))
            city_hit = bool(city) and _norm(city) == _norm(p.get("city", ""))
            # Un artiste joue plusieurs fois par mois en tournée : une date
            # « proche » ne prouve rien. On n'accepte que sur une preuve —
            # même date, même salle ou même ville. Sinon on préfère ne rien
            # proposer (le gestionnaire colle le lien setlist.fm exact).
            if not (exact or venue_hit or city_hit):
                continue
            key = _flex_score(p, target, venue, city)
            if best_key is None or key > best_key:
                best, best_key = p, key
    return best
