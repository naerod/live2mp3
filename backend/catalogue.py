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
        has_traycard = (pdir / "artwork" / "tray_card.pdf").exists()
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
            "has_traycard": has_traycard,
            "labels": _labels(album, has_mp3, has_mp4),
        })
    return albums


def _labels(album: dict, has_mp3: bool, has_mp4: bool) -> list[str]:
    """Labels de l'album : ceux du manifest + dérivés de la disponibilité média."""
    labels = list(album.get("labels", []) or [])
    if has_mp3 and has_mp4:
        media = "audio + vidéo"
    elif has_mp4:
        media = "vidéo"
    elif has_mp3:
        media = "audio"
    else:
        media = None
    if media and media not in labels:
        labels.insert(0, media)
    # dédoublonne en gardant l'ordre
    seen, out = set(), []
    for l in labels:
        k = l.lower()
        if k not in seen:
            seen.add(k)
            out.append(l)
    return out


def all_labels() -> list[str]:
    """Union triée de tous les labels du catalogue (pour les filtres)."""
    s: set[str] = set()
    for a in list_albums():
        s.update(a.get("labels", []))
    return sorted(s, key=str.lower)
