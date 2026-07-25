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
