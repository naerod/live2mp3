"""Suggestions canoniques pour les champs de profil (ville, artiste favori).

Objectif : un seul formalisme. Le client n'envoie jamais un libellé libre, il
envoie l'**identifiant** de l'entrée choisie (code INSEE, id Deezer) ; le
libellé stocké est toujours reconstruit ici à partir de la source. Deux
utilisateurs qui désignent la même ville ont donc forcément la même chaîne, et
un futur regroupement (« les fans de X », « autour de Dijon ») peut se faire sur
l'id plutôt que sur le texte.

Sources — publiques, sans clé, sans quota déclaré :
- villes    : geo.api.gouv.fr (communes françaises, code INSEE + codes postaux)
- artistes  : api.deezer.com (classement par popularité, meilleur pour de
  l'autocomplétion que MusicBrainz qui trie par score lexical)

Réseau tiers = faillible : la recherche renvoie une liste vide en cas de panne
(le champ reste utilisable, simplement sans suggestion) et la résolution lève
`SourceUnavailable` pour que l'appelant distingue « id invalide » de « source
injoignable ».
"""
from __future__ import annotations

import threading
import time
from typing import Any

import requests

GEO_URL = "https://geo.api.gouv.fr"
DEEZER_URL = "https://api.deezer.com"
TIMEOUT = 4.0
SEARCH_TTL = 3600        # 1 h — la pertinence bouge peu
RESOLVE_TTL = 6 * 3600   # 6 h — un libellé canonique bouge encore moins
MAX_RESULTS = 8

# Les trois seules communes françaises à arrondissements : leurs codes postaux
# sont ceux des arrondissements (75001…75020), jamais le code générique que
# tout le monde utilise pour désigner la ville entière. On le reconstruit.
_ARRONDISSEMENT_CITIES = {"75056": "75000", "69123": "69000", "13055": "13000"}


class SourceUnavailable(RuntimeError):
    """La source tierce n'a pas répondu — l'id n'est ni valide ni invalide."""


# --- Cache mémoire minimal (process-local, TTL) -----------------------------
_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


def _cache_get(key: str) -> Any | None:
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        _cache.pop(key, None)
    return None


def _cache_put(key: str, value: Any, ttl: int) -> None:
    with _lock:
        if len(_cache) > 2000:      # borne grossière : purge des entrées mortes
            now = time.monotonic()
            for k in [k for k, v in _cache.items() if v[0] <= now]:
                _cache.pop(k, None)
        _cache[key] = (time.monotonic() + ttl, value)


def _get_json(url: str, params: dict | None = None) -> Any:
    r = requests.get(url, params=params, timeout=TIMEOUT,
                     headers={"User-Agent": "live2mp3/1.0 (+https://live2mp3.naerod.com)"})
    r.raise_for_status()
    return r.json()


# --- Villes ----------------------------------------------------------------
def _city_postal(commune: dict) -> str:
    codes = commune.get("codesPostaux") or []
    generic = _ARRONDISSEMENT_CITIES.get(commune.get("code", ""))
    if generic:
        return generic
    return min(codes) if codes else ""


def _city_label(commune: dict) -> str:
    """« Dijon, 21000 » — le formalisme unique demandé."""
    cp = _city_postal(commune)
    return f"{commune['nom']}, {cp}" if cp else commune["nom"]


def search_cities(q: str) -> list[dict]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    key = f"city:s:{q.lower()}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        rows = _get_json(f"{GEO_URL}/communes", {
            "nom": q,
            "fields": "nom,code,codesPostaux,departement",
            "boost": "population",
            "limit": MAX_RESULTS,
        })
    except Exception:
        return []
    out = [{
        "id": c["code"],
        "label": _city_label(c),
        # Les homonymes sont légion (14 « Sainte-Colombe ») : le département
        # départage à l'œil, le code INSEE départage en base.
        "hint": (c.get("departement") or {}).get("nom", ""),
    } for c in rows if c.get("nom")]
    _cache_put(key, out, SEARCH_TTL)
    return out


def resolve_city(code: str) -> dict | None:
    """Code INSEE → entrée canonique. None si le code n'existe pas."""
    code = (code or "").strip()
    if not code:
        return None
    key = f"city:r:{code}"
    cached = _cache_get(key)
    if cached is not None:
        return cached or None
    try:
        c = _get_json(f"{GEO_URL}/communes/{code}",
                      {"fields": "nom,code,codesPostaux,departement"})
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            _cache_put(key, {}, RESOLVE_TTL)
            return None
        raise SourceUnavailable("geo.api.gouv.fr") from e
    except Exception as e:
        raise SourceUnavailable("geo.api.gouv.fr") from e
    out = {"id": c["code"], "label": _city_label(c),
           "hint": (c.get("departement") or {}).get("nom", "")}
    _cache_put(key, out, RESOLVE_TTL)
    return out


# --- Artistes --------------------------------------------------------------
def search_artists(q: str) -> list[dict]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    key = f"art:s:{q.lower()}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        # On demande large : le dédoublonnage ci-dessous retire des entrées.
        data = _get_json(f"{DEEZER_URL}/search/artist",
                         {"q": q, "limit": MAX_RESULTS * 3})
    except Exception:
        return []

    # Deezer laisse coexister des homonymes exacts : le vrai « Coldplay »
    # (122 albums, 18M fans) et un squatteur vide du même nom. Deux entrées
    # identiques à l'écran = deux ids possibles pour un même libellé, soit
    # exactement le formalisme éclaté qu'on cherche à éviter. On ne garde donc
    # qu'un artiste par nom — le plus suivi — et on écarte les fiches vides.
    best: dict[str, dict] = {}
    for a in (data.get("data") or []):
        name = (a.get("name") or "").strip()
        if not name:
            continue
        if not a.get("nb_album") and (a.get("nb_fan") or 0) < 1000:
            continue
        k = name.casefold()
        if k not in best or (a.get("nb_fan") or 0) > (best[k].get("nb_fan") or 0):
            best[k] = a

    out = [{
        "id": str(a["id"]),
        "label": a["name"],
        "picture": a.get("picture_small") or "",
    } for a in sorted(best.values(), key=lambda x: x.get("nb_fan") or 0, reverse=True)
    ][:MAX_RESULTS]
    _cache_put(key, out, SEARCH_TTL)
    return out


def resolve_artist(artist_id: str) -> dict | None:
    """Id Deezer → entrée canonique. None si l'artiste n'existe pas."""
    artist_id = (artist_id or "").strip()
    if not artist_id.isdigit():
        return None
    key = f"art:r:{artist_id}"
    cached = _cache_get(key)
    if cached is not None:
        return cached or None
    try:
        a = _get_json(f"{DEEZER_URL}/artist/{artist_id}")
    except Exception as e:
        raise SourceUnavailable("api.deezer.com") from e
    # Deezer répond 200 avec un objet `error` pour un id inconnu.
    if a.get("error") or not a.get("name"):
        _cache_put(key, {}, RESOLVE_TTL)
        return None
    out = {"id": str(a["id"]), "label": a["name"],
           "picture": a.get("picture_small") or ""}
    _cache_put(key, out, RESOLVE_TTL)
    return out
