"""Rafraîchissement de la bibliothèque Jellyfin après un re-rendu.

Sur CT110, `sync-media.sh` (cron) monte les albums publiés dans Jellyfin via
des symlinks vers `build/audio`/`build/video` et ne déclenche un scan que
lorsqu'un symlink apparaît ou disparaît (publication/dépublication). Un
re-rendu de contenu à l'intérieur d'un dossier déjà monté (ré-édition d'un
album déjà publié) n'est donc jamais détecté sans appel explicite.

Configuration par variables d'environnement (réseau Docker `apps_web`,
live2mp3-app et jellyfin sont déjà colocalisés) :
- `JELLYFIN_URL` : défaut `http://jellyfin:8096` ;
- `JELLYFIN_API_KEY` : clé d'API Jellyfin (même clé que sync-media.sh,
  `/opt/apps/jellyfin/apikey` sur CT110).

Sans clé, `refresh_library()` est un no-op silencieux (best-effort, comme
`notifications.announce_post`) : ne doit jamais faire échouer un rendu.
"""
from __future__ import annotations

import os

import requests

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://jellyfin:8096").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "").strip()

_TIMEOUT = 10


def refresh_library() -> bool:
    """Déclenche un scan Jellyfin. Best-effort : ne lève jamais."""
    if not JELLYFIN_API_KEY:
        return False
    try:
        r = requests.post(
            f"{JELLYFIN_URL}/Library/Refresh",
            headers={"X-Emby-Token": JELLYFIN_API_KEY},
            timeout=_TIMEOUT,
        )
        return r.status_code < 400
    except requests.RequestException:
        return False


def _find_album_id(slug: str) -> str | None:
    """Retourne l'Id Jellyfin de l'album monté sous `.../musique/<slug>`."""
    r = requests.get(
        f"{JELLYFIN_URL}/Items",
        params={
            "Recursive": "true",
            "IncludeItemTypes": "MusicAlbum",
            "Fields": "Path",
        },
        headers={"X-Emby-Token": JELLYFIN_API_KEY},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    suffix = f"/musique/{slug}"
    for item in r.json().get("Items", []):
        path = item.get("Path") or ""
        if path.rstrip("/").endswith(suffix):
            return item.get("Id")
    return None


def refresh_album(slug: str) -> bool:
    """Force Jellyfin à re-scanner un album ET à ré-extraire ses images.

    Un `Library/Refresh` ordinaire ne ré-extrait pas l'art déjà en cache : sur
    une simple mise à jour de pochette (contenu inchangé par ailleurs) l'image
    reste figée côté serveur, donc côté Finamp. On cible l'album par son
    chemin et on impose `ReplaceAllImages` + `FullRefresh` (récursif pour
    couvrir les pistes, utile aux albums à pochette par piste).

    Best-effort : ne lève jamais. Retourne False si pas de clé, album
    introuvable ou erreur réseau.
    """
    if not JELLYFIN_API_KEY:
        return False
    try:
        item_id = _find_album_id(slug)
        if not item_id:
            return False
        r = requests.post(
            f"{JELLYFIN_URL}/Items/{item_id}/Refresh",
            params={
                "MetadataRefreshMode": "FullRefresh",
                "ImageRefreshMode": "FullRefresh",
                "ReplaceAllImages": "true",
                "ReplaceAllMetadata": "false",
                "Recursive": "true",
            },
            headers={"X-Emby-Token": JELLYFIN_API_KEY},
            timeout=_TIMEOUT,
        )
        return r.status_code < 400
    except requests.RequestException:
        return False
