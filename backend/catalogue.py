"""Catalogue des albums live traités — alimente la vitrine publique.

Scanne `projects/` : chaque dossier avec un manifest et des rendus disponibles
devient une entrée (métadonnées + disponibilité MP3/MP4 + cover).
"""
from __future__ import annotations

from pathlib import Path

from .manifest import Manifest, PROJECTS_DIR


def _has_files(d: Path, ext: str) -> bool:
    return d.exists() and any(d.glob(f"*.{ext}"))


def list_albums() -> list[dict]:
    albums: list[dict] = []
    if not PROJECTS_DIR.exists():
        return albums
    for pdir in sorted(PROJECTS_DIR.iterdir()):
        manifest = pdir / "manifest.yaml"
        if not manifest.is_file():
            continue
        try:
            m = Manifest.load(manifest)
        except Exception:
            continue
        album = m.data.get("album", {})
        has_mp3 = _has_files(pdir / "build" / "audio", "mp3")
        has_mp4 = _has_files(pdir / "build" / "video", "mp4")
        if not (has_mp3 or has_mp4):
            continue  # rien de publiable encore
        cover_rel = album.get("cover")
        has_cover = bool(cover_rel and (pdir / cover_rel).exists())
        albums.append({
            "slug": pdir.name,
            "artist": album.get("artist", ""),
            "title": album.get("title", ""),
            "date": album.get("date", ""),
            "venue": album.get("venue", ""),
            "festival": album.get("festival", ""),
            "tracks": len(m.tracks),
            "has_mp3": has_mp3,
            "has_mp4": has_mp4,
            "has_cover": has_cover,
        })
    return albums
