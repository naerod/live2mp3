"""Opérations sur les fichiers d'un album : nommage, tags ID3, pochette.

Partagé entre l'édition d'un album existant (`main.py`) et l'import d'un
nouvel album (`import_album.py`) : les deux doivent produire exactement les
mêmes noms de fichiers et les mêmes tags.
"""
from __future__ import annotations

import re
from pathlib import Path

from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC, ID3NoHeaderError

from . import manifest as _manifest
from .manifest import Manifest


def _projects_dir():
    """Racine des projets, résolue à l'appel.

    `manifest.PROJECTS_DIR` est la source unique : l'importer par valeur ici
    figerait le chemin à l'import et échapperait au patch des tests.
    """
    return _manifest.PROJECTS_DIR

COVER_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".png": "image/png", ".webp": "image/webp"}
MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _file_track_n(stem: str) -> int | None:
    """Extrait le numéro de piste depuis le nom de fichier MP3.

    Supporte les formats :
      - '01_Overcompensate'  → 1
      - '1. Pour Me'         → 1
      - '14. ALiENS'         → 14
    """
    m = re.match(r"^(\d+)[._\s]", stem)
    return int(m.group(1)) if m else None


def _sanitize_filename(title: str) -> str:
    """Supprime les caractères interdits dans un nom de fichier.

    Délègue à `manifest.sanitize_filename` — source unique de la convention
    « 01. Titre.mp3 », partagée avec le pipeline de rendu.
    """
    return _manifest.sanitize_filename(title)


def _rename_audio_files(slug: str, m: Manifest) -> int:
    """Renomme les MP3 au format '01. Titre.mp3' en suivant l'ordre du manifest."""
    audio_dir = _projects_dir() / slug / "build" / "audio"
    if not audio_dir.exists():
        return 0
    rename_map: dict[int, tuple[int, str]] = {}
    for i, t in enumerate(m.tracks):
        safe = _sanitize_filename(t.get("title", "") or f"Track {t['n']}")
        rename_map[int(t["n"])] = (i + 1, safe)
    # Passe 1 : renommer vers un nom temporaire pour éviter les collisions
    pending: dict[Path, Path] = {}
    for mp3_path in list(audio_dir.glob("*.mp3")):
        file_n = _file_track_n(mp3_path.stem)
        if file_n is None or file_n not in rename_map:
            continue
        new_pos, safe_title = rename_map[file_n]
        new_name = f"{new_pos:02d}. {safe_title}.mp3"
        if mp3_path.name == new_name:
            continue
        tmp_path = mp3_path.parent / f"._tmp_{file_n}_{mp3_path.name}"
        mp3_path.rename(tmp_path)
        pending[tmp_path] = mp3_path.parent / new_name
    # Passe 2 : renommer vers le nom final
    for tmp, final in pending.items():
        tmp.rename(final)
    return len(pending)


def _write_album_tags(slug: str, m: Manifest) -> int:
    """Écrit les tags ID3 communs à tout l'album (TALB, TPE1, TDRC) dans tous les MP3."""
    audio_dir = _projects_dir() / slug / "build" / "audio"
    if not audio_dir.exists():
        return 0
    alb = m.data.get("album", {})
    album_title = alb.get("title", "") or ""
    artist = alb.get("artist", "") or ""
    date = alb.get("date", "") or ""
    year = date[:4] if len(date) >= 4 else date
    tagged = 0
    for mp3_path in audio_dir.glob("*.mp3"):
        try:
            try:
                tags = EasyID3(str(mp3_path))
            except ID3NoHeaderError:
                tags = EasyID3()
                tags.save(str(mp3_path))
                tags = EasyID3(str(mp3_path))
            if album_title:
                tags["album"] = [album_title]
            if artist:
                tags["artist"] = [artist]
                tags["albumartist"] = [artist]
            if year:
                tags["date"] = [year]
            tags.save()
            tagged += 1
        except Exception:
            pass
    return tagged


def _write_track_tags(slug: str, m: Manifest) -> int:
    """Écrit les tags par piste (TIT2, TRCK) en associant fichier ↔ manifest."""
    audio_dir = _projects_dir() / slug / "build" / "audio"
    if not audio_dir.exists():
        return 0
    # n d'origine (celui du nom de fichier) → (position dans le manifest, titre),
    # même convention que l'édition de pistes : le tracknumber suit l'ordre affiché.
    pos_by_n = {int(t["n"]): (i + 1, t.get("title", "")) for i, t in enumerate(m.tracks)}
    total = len(m.tracks)
    tagged = 0
    for mp3_path in sorted(audio_dir.glob("*.mp3")):
        file_n = _file_track_n(mp3_path.stem)
        if file_n is None or file_n not in pos_by_n:
            continue
        pos, title = pos_by_n[file_n]
        try:
            try:
                tags = EasyID3(str(mp3_path))
            except ID3NoHeaderError:
                tags = EasyID3()
                tags.save(str(mp3_path))
                tags = EasyID3(str(mp3_path))
            tags["title"] = [title]
            tags["tracknumber"] = [f"{pos}/{total}"]
            tags.save()
            tagged += 1
        except Exception:
            pass
    return tagged


def _write_album_cover(slug: str, m: Manifest) -> int:
    """Embarque la pochette du site (album.cover) comme APIC dans tous les MP3."""
    audio_dir = _projects_dir() / slug / "build" / "audio"
    cover_rel = m.data.get("album", {}).get("cover")
    if not audio_dir.exists() or not cover_rel:
        return 0
    cover_path = _projects_dir() / slug / cover_rel
    if not cover_path.exists():
        return 0
    data = cover_path.read_bytes()
    mime = COVER_MIME.get(cover_path.suffix.lower(), "image/jpeg")
    embedded = 0
    for mp3_path in audio_dir.glob("*.mp3"):
        try:
            try:
                tags = ID3(str(mp3_path))
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            tags.save(str(mp3_path))
            embedded += 1
        except Exception:
            pass
    return embedded


def _extract_embedded_cover(slug: str, m: Manifest) -> str | None:
    """Si aucune cover site mais un APIC embarqué existe, l'extrait vers artwork/cover.*.

    Sert au rattrapage des albums importés (pochette déjà dans les MP3, absente du site).
    Retourne le chemin relatif de la cover créée, ou None.
    """
    if m.data.get("album", {}).get("cover"):
        return None
    audio_dir = _projects_dir() / slug / "build" / "audio"
    if not audio_dir.exists():
        return None
    for mp3_path in sorted(audio_dir.glob("*.mp3")):
        try:
            tags = ID3(str(mp3_path))
        except Exception:
            continue
        apics = tags.getall("APIC")
        if not apics:
            continue
        apic = apics[0]
        ext = MIME_EXT.get(apic.mime, ".jpg")
        art_dir = _projects_dir() / slug / "artwork"
        art_dir.mkdir(exist_ok=True)
        cover_path = art_dir / f"cover{ext}"
        cover_path.write_bytes(apic.data)
        m.data.setdefault("album", {})["cover"] = f"artwork/cover{ext}"
        m.save()
        return f"artwork/cover{ext}"
    return None
