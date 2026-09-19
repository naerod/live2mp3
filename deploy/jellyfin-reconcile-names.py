#!/usr/bin/env python3
"""Filet de sécurité : réconcilie les noms d'album Jellyfin avec live2mp3.

Le chemin normal est applicatif : live2mp3 réécrit les tags ID3 puis appelle
`/Items/{id}/Refresh?ReplaceAllMetadata=true` (backend/jellyfin.py). Ce script
ne sert qu'au rattrapage : si un renommage passe à côté de ce chemin (appel
best-effort raté, Jellyfin indisponible au moment de l'édition, modification
hors application), l'album resterait indéfiniment sous son ancien nom dans
Finamp — le cache client n'étant jamais invalidé puisque Jellyfin lui-même ne
bouge pas.

Compare, pour chaque album monté dans /media/musique/<slug>, le `Name` indexé
par Jellyfin au `album.title` du manifeste, et force un refresh ciblé en cas
d'écart. Idempotent, silencieux quand tout est cohérent (cas courant).

Surveille aussi les albums dont l'**artiste** a disparu côté Jellyfin. Ce champ
n'existe dans aucun fichier : Jellyfin le dérive des pistes, et un refresh
`ReplaceAllMetadata=true` l'efface — l'album repasse alors « Unknown Artist »
dans Finamp. Un refresh non destructif le reconstruit.

Déployé sur CT110 : /opt/apps/jellyfin/reconcile-names.py, cron horaire.
Incident fondateur : 2026-09-19 (album TIF resté « Olympia 2024 » dans Finamp).
"""
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

# Jellyfin ne publie pas 8096 sur l'hôte : on passe par nginx local en
# forçant le Host du vhost (même technique que sync-media.sh pour le catalogue).
JELLYFIN = "http://127.0.0.1"
HOST_HEADER = "music.naerod.com"
PROJECTS = Path("/opt/data/live2mp3/projects")
KEY = Path("/opt/apps/jellyfin/apikey").read_text().strip()
# Conteneur applicatif : seul endroit où vivent mutagen et le code de tagging.
APP_CONTAINER = "live2mp3-app"


def _call(method, path, params=None):
    url = f"{JELLYFIN}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, method=method,
        headers={"X-Emby-Token": KEY, "Host": HOST_HEADER},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return json.loads(body) if body else None


def manifest_title(slug):
    path = PROJECTS / slug / "manifest.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    return ((data.get("album") or {}).get("title") or "").strip() or None


def retag(slug):
    """Réaligne les tags ID3 sur le manifeste avant de rafraîchir Jellyfin.

    Un écart de nom peut venir de deux endroits : Jellyfin en retard sur des
    tags corrects (cas courant), ou des tags eux-mêmes restés en arrière — et
    dans ce second cas un refresh seul ne converge jamais, Jellyfin relisant
    l'ancien titre à chaque passage. On réécrit donc les tags d'abord, via le
    code applicatif lui-même (mutagen n'existe que dans le conteneur).
    """
    code = (
        "from backend.manifest import Manifest, PROJECTS_DIR;"
        "from backend.albumfiles import _write_album_tags;"
        f"print(_write_album_tags({slug!r}, "
        f"Manifest.load(PROJECTS_DIR / {slug!r} / 'manifest.yaml')))"
    )
    r = subprocess.run(
        ["docker", "exec", APP_CONTAINER, "python", "-c", code],
        capture_output=True, text=True, timeout=120,
    )
    return r.returncode == 0


def main():
    items = _call("GET", "/Items", {
        "Recursive": "true", "IncludeItemTypes": "MusicAlbum",
        "Fields": "Path,AlbumArtist",
    })["Items"]
    fixed = []
    rederived = []
    for item in items:
        path = (item.get("Path") or "").rstrip("/")
        if "/musique/" not in path:
            continue
        slug = path.rsplit("/", 1)[-1]

        # Artiste dérivé perdu : un simple refresh non destructif le rétablit.
        if not item.get("AlbumArtist"):
            _call("POST", f"/Items/{item['Id']}/Refresh", {
                "MetadataRefreshMode": "FullRefresh",
                "ImageRefreshMode": "None",
                "ReplaceAllMetadata": "false",
                "Recursive": "true",
            })
            rederived.append(slug)

        expected = manifest_title(slug)
        if not expected or item.get("Name") == expected:
            continue
        retag(slug)
        _call("POST", f"/Items/{item['Id']}/Refresh", {
            "MetadataRefreshMode": "FullRefresh",
            "ImageRefreshMode": "FullRefresh",
            "ReplaceAllImages": "true",
            "ReplaceAllMetadata": "true",
            "Recursive": "true",
        })
        # Le passage ci-dessus efface artiste et année (champs dérivés) :
        # on les reconstruit aussitôt, sans toucher au nom corrigé.
        _call("POST", f"/Items/{item['Id']}/Refresh", {
            "MetadataRefreshMode": "FullRefresh",
            "ImageRefreshMode": "None",
            "ReplaceAllMetadata": "false",
            "Recursive": "true",
        })
        fixed.append(f"{slug}: {item.get('Name')!r} -> {expected!r}")
    if rederived:
        print(f"{len(rederived)} album(s) sans artiste, métadonnées "
              f"reconstruites : {', '.join(rederived)}")
    if fixed:
        print(f"{len(fixed)} album(s) réconcilié(s) :")
        for line in fixed:
            print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
