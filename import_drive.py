#!/usr/bin/env python3
"""Import albums from Google Drive → live2mp3-preprod (CT110).

Usage:
  python3 import_drive.py               # import tous les albums
  python3 import_drive.py --dry-run     # simulation sans téléchargement
  python3 import_drive.py --slug SLUG   # un seul album
"""

from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Config ─────────────────────────────────────────────────────────────────────

CT110 = "root@192.168.1.110"
PROJECTS_DIR = "/var/lib/docker/volumes/live2mp3-preprod_projects/_data"
IMPORTED_BY = "naerod"
DRIVE_API = "https://www.googleapis.com/drive/v3"

# ── Catalogue des albums ────────────────────────────────────────────────────────
# audio_folder : ID du dossier Drive contenant les MP3 (+ éventuellement cover)
# video_folder : ID du dossier Drive contenant les MP4 (optionnel)
# cover_file_id: ID du fichier cover si pas dans audio_folder
# target       : "audio_cd" (MP3 seul) ou "data_disc" (MP3+MP4 ou MP4 seul)

ALBUMS = [
    # ── Indochine ──────────────────────────────────────────────────────────────
    {
        "artist": "Indochine",
        "title": "Bercy Arena Tour - Live",
        "date": "2026-03-04",
        "venue": "Accor Arena, Paris",
        "festival": "",
        "target": "data_disc",
        "audio_folder": "1vIx7TVrQJyDrWZvWMaj7ISAA_6OLrMoB",
        "video_folder": "1kv-UsmI6PtrWpmeshJW_om4Q40RhJn1E",
    },

    # ── U2 ─────────────────────────────────────────────────────────────────────
    {
        "artist": "U2",
        "title": "Vertigo Tour 2006 - Live",
        "date": "2006",
        "venue": "",
        "festival": "",
        "target": "audio_cd",
        "audio_folder": "1BmVlw0dlToKbSC2mn67UaNdwqY9u5hhC",
    },

    # ── Twenty One Pilots ──────────────────────────────────────────────────────
    {
        "artist": "Twenty One Pilots",
        "title": "Live at Southside Music Festival",
        "date": "2022-06-17",
        "venue": "",
        "festival": "Southside Music Festival",
        "target": "audio_cd",
        "audio_folder": "1bwE83xKp-57O5Z_pWOS5JynWnlb10-yQ",
    },
    {
        "artist": "Twenty One Pilots",
        "title": "March Madness Music Festival — AT&T Block Party 2026",
        "date": "2026-04-05",
        "venue": "",
        "festival": "March Madness Music Festival",
        "target": "audio_cd",
        "audio_folder": "144et_owUiYXMper-Pa6Z5gafFra_cUje",
    },

    # ── Linkin Park ────────────────────────────────────────────────────────────
    {
        "artist": "Linkin Park",
        "title": "FROM ZERO (Livestream)",
        "date": "2024-09-05",
        "venue": "",
        "festival": "",
        "target": "data_disc",
        "audio_folder": "1fECRb6piHCTuC9jOSHfJVPFTmI5n-uu1",
        "video_folder": "1zA100k7saMHEUpuAZ7IRvuh5u40CK47e",
    },
    {
        "artist": "Linkin Park",
        "title": "Live in São Paulo — FROM ZERO World Tour",
        "date": "2024-11-16",
        "venue": "Allianz Parque, São Paulo",
        "festival": "",
        "target": "data_disc",
        "audio_folder": "1xoOWHZSg79wwJebnmBIytAqmbkODL5C6",
        "video_folder": "1X8NiDySYvAN5-nBPaQL1kZFwSbHifTWv",
    },

    # ── Coldplay ───────────────────────────────────────────────────────────────
    {
        "artist": "Coldplay",
        "title": "Ghost Stories Live 2014",
        "date": "2014",
        "venue": "",
        "festival": "",
        "target": "data_disc",
        "video_folder": "1mB_Wjnt5TA9XclyEFmQ7KhzqADlPNs_a",
    },
    {
        "artist": "Coldplay",
        "title": "Live at Glastonbury 2011",
        "date": "2011-06-26",
        "venue": "Glastonbury Festival",
        "festival": "Glastonbury",
        "target": "audio_cd",
        "audio_folder": "18bXI5yJfvg6pL0Q2wsbINTEgW_pku_vn",
    },
    {
        "artist": "Coldplay",
        "title": "Live at Glastonbury 2024",
        "date": "2024-06-30",
        "venue": "Glastonbury Festival",
        "festival": "Glastonbury",
        "target": "audio_cd",
        "audio_folder": "17b0tFiwpoqpAQtg2gmXN0C04xrQ6tUYI",
        # Cover dans le dossier parent
        "cover_file_id": "1glHb-jAeSvgbPlw32FopBOzarOyl1W2f",
        "cover_filename": "cover_glastonbury2024.jpg",
    },
    {
        "artist": "Coldplay",
        "title": "Live in Wembley — Music of the Spheres Tour",
        "date": "2022-06-01",
        "venue": "Wembley Stadium, London",
        "festival": "",
        "target": "audio_cd",
        "audio_folder": "1uSbIyZN9ZSzPlzAvn1bsnn1MyTTCFBMr",
    },
    {
        "artist": "Coldplay",
        "title": "Live in Toronto 2006 — X&Y Tour",
        "date": "2006-08",
        "venue": "Air Canada Centre, Toronto",
        "festival": "",
        "target": "audio_cd",
        "audio_folder": "1KFbN_NEUiUk2KbnCW_VOeJWo1cBzF2g4",
    },
    {
        "artist": "Coldplay",
        "title": "Live in Tokyo 2009 — Viva la Vida Tour",
        "date": "2009",
        "venue": "Saitama Super Arena, Tokyo",
        "festival": "",
        "target": "audio_cd",
        "audio_folder": "1I2RAIJmcIH9go8g6fwQtPB6k9IFwqXxT",
    },
    {
        "artist": "Coldplay",
        "title": "Live 2012",
        "date": "2012",
        "venue": "",
        "festival": "",
        "target": "audio_cd",
        "audio_folder": "1lRUUA52rHBb2IAGtnxnWjysHEltP7fz9",
    },
    {
        "artist": "Coldplay",
        "title": "Live in Paris — Music of the Spheres Tour",
        "date": "2022-09-25",
        "venue": "Stade de France, Paris",
        "festival": "",
        "target": "audio_cd",
        "audio_folder": "1vDYb9PYmZhBngDmCrQ_lSARwHlbQpweA",
    },
    {
        "artist": "Coldplay",
        "title": "Everyday Life — Live in Jordan",
        "date": "2019-11-22",
        "venue": "Amman Amphitheatre, Amman",
        "festival": "",
        "target": "audio_cd",
        "audio_folder": "18Kd1wOStEhb5VboM736aNa9KJpqqK7Za",
    },
    {
        "artist": "Coldplay",
        "title": "MOON MUSiC Tour Edition",
        "date": "2024-10-04",
        "venue": "",
        "festival": "",
        "target": "audio_cd",
        "audio_folder": "1gh_TOAkCL4hgu1wcN4RCEoGWo395DmsE",
    },
    {
        "artist": "Coldplay",
        "title": "ALiEN RADiO (Unreleased)",
        "date": "2025",
        "venue": "",
        "festival": "",
        "target": "audio_cd",
        "audio_folder": "1h9euDnBqLKvNlriAZEMFNZNc215hbvhT",
    },
    {
        "artist": "Coldplay",
        "title": "Live from Spotify London",
        "date": "2024-11",
        "venue": "Spotify London",
        "festival": "",
        "target": "audio_cd",
        "audio_folder": "1e6w4QSna4iN3geu8QJrbDp-b9QBZVha4",
    },
]

# ── Token ──────────────────────────────────────────────────────────────────────
# Utilise le token OAuth du MCP Google Workspace (scope drive inclus)

TOKEN_JSON = Path.home() / ".google-workspace-mcp/token.json"
CREDS_JSON = Path.home() / ".google-workspace-mcp/credentials.json"

_token_cache: dict = {"token": None, "ts": 0}

def _refresh_token() -> str:
    """Rafraîchit le token via l'endpoint OAuth."""
    t = json.loads(TOKEN_JSON.read_text())
    c = json.loads(CREDS_JSON.read_text())["installed"]
    r = subprocess.run(
        ["curl", "-s", "-X", "POST", c["token_uri"],
         "-d", f"client_id={c['client_id']}&client_secret={c['client_secret']}"
               f"&refresh_token={t['refresh_token']}&grant_type=refresh_token"],
        capture_output=True, text=True, check=True
    )
    data = json.loads(r.stdout)
    if "access_token" not in data:
        raise RuntimeError(f"Token refresh failed: {data.get('error')}")
    # Mettre à jour le fichier token
    t["access_token"] = data["access_token"]
    t["expiry_date"] = int(time.time() * 1000) + data.get("expires_in", 3600) * 1000
    TOKEN_JSON.write_text(json.dumps(t))
    return data["access_token"]

def get_token() -> str:
    if _token_cache["token"] and (time.time() - _token_cache["ts"]) < 3000:
        return _token_cache["token"]
    t = json.loads(TOKEN_JSON.read_text())
    expiry_s = t.get("expiry_date", 0) / 1000
    if expiry_s - time.time() < 300:
        token = _refresh_token()
        print("  🔑 Token refreshed (OAuth)")
    else:
        token = t["access_token"]
    _token_cache["token"] = token
    _token_cache["ts"] = time.time()
    return token

# ── Drive API ──────────────────────────────────────────────────────────────────

def drive_list_folder(folder_id: str) -> list[dict]:
    from urllib.parse import urlencode
    token = get_token()
    files: list[dict] = []
    page_token: Optional[str] = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "fields": "nextPageToken,files(id,name,mimeType)",
            "pageSize": "1000",
        }
        if page_token:
            params["pageToken"] = page_token
        url = f"{DRIVE_API}/files?{urlencode(params)}"
        r = subprocess.run(
            ["curl", "-s", "-H", f"Authorization: Bearer {token}", url],
            capture_output=True, text=True, check=True
        )
        data = json.loads(r.stdout)
        if "error" in data:
            print(f"  ❌ Drive API error: {data['error'].get('message')}")
            return []
        files.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return files

def drive_download_to_ct110(file_id: str, dest: str, token: str) -> bool:
    url = f"{DRIVE_API}/files/{file_id}?alt=media"
    cmd = f"curl -s -f -L -H 'Authorization: Bearer {token}' '{url}' -o '{dest}'"
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", CT110, cmd],
                       capture_output=True, text=True)
    return r.returncode == 0

# ── SSH helpers ────────────────────────────────────────────────────────────────

def ssh(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", "-o", "BatchMode=yes", CT110, cmd],
                          capture_output=True, text=True)

def ssh_mkdir(path: str) -> None:
    ssh(f"mkdir -p '{path}'")

def ssh_write(content: str, dest: str) -> None:
    subprocess.run(["ssh", "-o", "BatchMode=yes", CT110, f"cat > '{dest}'"],
                   input=content, text=True, check=True)

# ── Filename helpers ───────────────────────────────────────────────────────────

def slugify(value: str) -> str:
    value = re.sub(r"[·–—/]", " ", value)
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "untitled"

def project_slug(artist: str, date: str) -> str:
    return f"{slugify(artist)}-{slugify(str(date))}"

def parse_track_n(name: str) -> int:
    stem = re.sub(r"^\[.*?\]\s*", "", Path(name).stem)
    for pat in [r"^(\d{1,2})\.", r"^(\d{1,2})\s*[-_]\s*", r"^0*(\d+)\s",
                r"_(\d{2})_", r"^(\d{1,2})$"]:
        m = re.match(pat, stem)
        if m:
            return int(m.group(1))
    return 0

def parse_track_title(name: str) -> str:
    stem = re.sub(r"^\[.*?\]\s*", "", Path(name).stem)
    stem = re.sub(r"^\d{1,2}\.\s*", "", stem)
    stem = re.sub(r"^\d{1,2}\s*-\s*", "", stem)
    stem = re.sub(r"^\d{1,2}_", "", stem)
    stem = re.sub(r"^\d{2}_\d{2}_[A-Z]+_", "", stem)  # 01_02_COLDPLAY_
    stem = re.sub(r"^\d{1,2}\s+", "", stem)
    return stem.strip() or name

def is_skip_file(name: str) -> bool:
    ext = Path(name).suffix.lower()
    if ext in (".pdf", ".doc", ".docx", ".txt", ".nfo", ".wav", ".m4a"):
        return True
    stem = Path(name).stem.lower()
    # Fichiers de concert complet (track 00)
    if re.match(r"^0+_", stem) or re.match(r"^00_full", stem):
        return True
    return False

def is_image(mime: str) -> bool:
    return mime.startswith("image/")

def is_audio(mime: str) -> bool:
    return mime.startswith("audio/") or mime == "application/octet-stream"

def is_video(mime: str) -> bool:
    return mime.startswith("video/")

def is_cover_candidate(name: str) -> bool:
    stem = Path(name).stem.lower()
    return any(k in stem for k in ["cover", "jacket", "artwork", "front", "lp_live"])

# ── Manifest YAML ──────────────────────────────────────────────────────────────

def build_manifest(album_def: dict, tracks: list[dict], cover_name: Optional[str]) -> str:
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    cover_line = f"  cover: artwork/{cover_name}" if cover_name else "  cover: null"
    target = album_def.get("target", "audio_cd")
    artwork_state = "done" if cover_name else "pending"

    def esc(s: str) -> str:
        return str(s).replace("'", "''")

    tracks_yaml = ""
    if tracks:
        for t in sorted(tracks, key=lambda x: x["n"]):
            tracks_yaml += (f"  - n: {t['n']}\n"
                            f"    title: '{esc(t['title'])}'\n"
                            f"    start: null\n"
                            f"    end: null\n"
                            f"    locked: false\n")
    else:
        tracks_yaml = "  - n: 1\n    title: Import\n    start: null\n    end: null\n    locked: false\n"

    return f"""album:
  artist: '{esc(album_def['artist'])}'
  title: '{esc(album_def['title'])}'
  date: '{esc(album_def.get('date', ''))}'
  venue: '{esc(album_def.get('venue', ''))}'
  festival: '{esc(album_def.get('festival', ''))}'
{cover_line}
  labels: []
meta:
  imported_by: '{IMPORTED_BY}'
  imported_at: '{now}'
target: {target}
source:
  url: ''
  master_mkv: source/master.mkv
  master_wav: source/master.wav
pipeline_state:
  download: done
  waveform: pending
  ai_markers: pending
  render: done
  tags: done
  artwork: {artwork_state}
  disc: pending
tracks:
{tracks_yaml}"""

# ── Import d'un album ──────────────────────────────────────────────────────────

def import_album(album_def: dict, dry_run: bool = False) -> bool:
    artist = album_def["artist"]
    title = album_def["title"]
    slug = project_slug(artist, album_def.get("date", ""))

    print(f"\n{'━'*62}")
    print(f"  {artist} — {title}")
    print(f"  slug: {slug}")
    print(f"{'━'*62}")

    base = f"{PROJECTS_DIR}/{slug}"
    audio_dst = f"{base}/build/audio"
    video_dst = f"{base}/build/video"
    art_dst = f"{base}/artwork"

    audio_files: list[dict] = []
    video_files: list[dict] = []
    cover_info: Optional[dict] = None  # {"id", "name"}

    # — Lister dossier audio —
    if album_def.get("audio_folder"):
        print(f"  📂 Audio folder…")
        files = drive_list_folder(album_def["audio_folder"])
        print(f"     {len(files)} fichiers")
        for f in files:
            if is_skip_file(f["name"]):
                continue
            if is_image(f["mimeType"]):
                if cover_info is None or is_cover_candidate(f["name"]):
                    cover_info = {"id": f["id"], "name": f["name"]}
            elif is_audio(f["mimeType"]):
                audio_files.append(f)
            elif is_video(f["mimeType"]):
                audio_files.append(f)  # vidéos dans le dossier audio → build/audio

    # — Lister dossier vidéo —
    if album_def.get("video_folder"):
        print(f"  📂 Video folder…")
        files = drive_list_folder(album_def["video_folder"])
        print(f"     {len(files)} fichiers")
        for f in files:
            if is_skip_file(f["name"]):
                print(f"     ⏭  {f['name']}")
                continue
            if is_image(f["mimeType"]) and cover_info is None:
                cover_info = {"id": f["id"], "name": f["name"]}
            elif is_video(f["mimeType"]):
                video_files.append(f)
            elif is_audio(f["mimeType"]) and f not in audio_files:
                audio_files.append(f)

    # — Cover explicite (ID connu) —
    if album_def.get("cover_file_id"):
        cover_info = {
            "id": album_def["cover_file_id"],
            "name": album_def.get("cover_filename", "cover.jpg"),
        }

    # — Vérification —
    if not audio_files and not video_files:
        print("  ❌ Aucun fichier audio/vidéo trouvé — album ignoré")
        return False

    # — Résumé —
    print(f"  Audio : {len(audio_files)} fichier(s)")
    print(f"  Vidéo : {len(video_files)} fichier(s)")
    print(f"  Cover : {cover_info['name'] if cover_info else 'aucune'}")

    if dry_run:
        print("  🔍 DRY RUN — rien téléchargé")
        return True

    # — Créer les dossiers sur CT110 —
    ssh_mkdir(audio_dst)
    ssh_mkdir(video_dst)
    ssh_mkdir(art_dst)

    errors = 0

    # — Télécharger la cover —
    if cover_info:
        token = get_token()
        dest = f"{art_dst}/{cover_info['name']}"
        ok = drive_download_to_ct110(cover_info["id"], dest, token)
        if not ok:
            print(f"  ⚠  Cover download failed: {cover_info['name']}")
            cover_info = None

    # — Télécharger les fichiers audio —
    for i, f in enumerate(audio_files, 1):
        token = get_token()
        dest = f"{audio_dst}/{f['name']}"
        ok = drive_download_to_ct110(f["id"], dest, token)
        print(f"  [Audio {i:3d}/{len(audio_files)}] {'✓' if ok else '✗'} {f['name'][:55]}")
        if not ok:
            errors += 1

    # — Télécharger les fichiers vidéo —
    for i, f in enumerate(video_files, 1):
        token = get_token()
        dest = f"{video_dst}/{f['name']}"
        ok = drive_download_to_ct110(f["id"], dest, token)
        print(f"  [Video {i:3d}/{len(video_files)}] {'✓' if ok else '✗'} {f['name'][:55]}")
        if not ok:
            errors += 1

    # — Construire la liste de pistes depuis les fichiers audio —
    tracks = []
    seen_n: set[int] = set()
    for f in sorted(audio_files, key=lambda x: x["name"]):
        n = parse_track_n(f["name"])
        while n in seen_n:
            n += 1
        seen_n.add(n)
        title = parse_track_title(f["name"])
        tracks.append({"n": n, "title": title})

    # — Écrire le manifest —
    manifest = build_manifest(
        album_def,
        tracks,
        cover_info["name"] if cover_info else None
    )
    ssh_write(manifest, f"{base}/manifest.yaml")
    print(f"  📝 manifest.yaml écrit")

    if errors:
        print(f"  ⚠  {errors} erreur(s) de téléchargement")
        return False

    print(f"  ✅ Album importé : {slug}")
    return True

# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Import Drive albums → live2mp3-preprod")
    parser.add_argument("--dry-run", action="store_true", help="Simulation sans download")
    parser.add_argument("--slug", help="N'importer qu'un album (slug artiste-date)")
    args = parser.parse_args()

    albums = ALBUMS
    if args.slug:
        albums = [a for a in ALBUMS
                  if project_slug(a["artist"], a.get("date", "")) == args.slug]
        if not albums:
            print(f"❌ Slug '{args.slug}' introuvable dans le catalogue.")
            sys.exit(1)

    print(f"{'═'*62}")
    print(f"  live2mp3 — Import Google Drive → preprod")
    print(f"  {len(albums)} album(s) à importer | dry_run={args.dry_run}")
    print(f"{'═'*62}")

    # Test connexion CT110
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                        CT110, "echo ok"], capture_output=True, text=True)
    if r.stdout.strip() != "ok":
        print("❌ Impossible de joindre CT110")
        sys.exit(1)

    ok_count = err_count = 0
    for album_def in albums:
        ok = import_album(album_def, dry_run=args.dry_run)
        if ok:
            ok_count += 1
        else:
            err_count += 1

    print(f"\n{'═'*62}")
    print(f"  Terminé — {ok_count} ✅  {err_count} ❌")
    print(f"{'═'*62}")

if __name__ == "__main__":
    main()
