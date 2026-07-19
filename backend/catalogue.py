"""Catalogue des albums live traités — alimente la vitrine publique.

Scanne `projects/` : chaque dossier avec un manifest et des rendus disponibles
devient une entrée (métadonnées + disponibilité MP3/MP4 + cover + import info).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import entities
from .db import get_conn
from .manifest import Manifest, PROJECTS_DIR


def _has_files(d: Path, ext: str) -> bool:
    return d.exists() and any(d.glob(f"*.{ext}"))


def _traycard_slugs() -> set[str]:
    """Slugs dont la pochette *gagnante* porte une tray card.

    Le classement est dupliqué ici (au lieu d'appeler `covers.rank_covers`) pour
    deux raisons : `covers` importe `catalogue` via `social`, donc l'importer en
    retour ferait un cycle ; et la vitrine liste tous les albums d'un coup, un
    appel par slug serait un N+1. Une seule requête à fenêtrage suffit.
    Doit rester aligné sur l'ordre de `covers.rank_covers`.
    """
    try:
        with get_conn() as conn:
            return {
                r["slug"]
                for r in conn.execute(
                    "SELECT slug, traycard_ext FROM ("
                    "  SELECT c.slug, c.traycard_ext, ROW_NUMBER() OVER ("
                    "    PARTITION BY c.slug"
                    "    ORDER BY c.pinned DESC, COUNT(l.cover_id) DESC,"
                    "             c.created_at ASC, c.id ASC"
                    "  ) AS rn"
                    "  FROM covers c LEFT JOIN cover_likes l ON l.cover_id = c.id"
                    "  GROUP BY c.id"
                    ") WHERE rn=1 AND traycard_ext != ''"
                )
            }
    except Exception:
        # La vitrine doit rester consultable même si la base sociale est
        # indisponible : au pire les badges tray card manquent.
        return set()


def list_albums(sort: str = "date_concert", include_drafts: bool = False) -> list[dict]:
    albums: list[dict] = []
    if not PROJECTS_DIR.exists():
        return albums
    tray_slugs = _traycard_slugs()
    for pdir in sorted(PROJECTS_DIR.iterdir()):
        manifest = pdir / "manifest.yaml"
        if not manifest.is_file():
            continue
        try:
            m = Manifest.load(manifest)
        except Exception:
            continue
        published = m.data.get("published", True)
        if not published and not include_drafts:
            continue
        album = m.data.get("album", {})
        has_mp3 = _has_files(pdir / "build" / "audio", "mp3")
        has_mp4 = _has_files(pdir / "build" / "video", "mp4")
        if not (has_mp3 or has_mp4):
            continue
        cover_rel = album.get("cover")
        has_cover = bool(cover_rel and (pdir / cover_rel).exists())
        has_traycard = pdir.name in tray_slugs
        meta = m.data.get("meta", {})
        albums.append({
            "slug": pdir.name,
            "artist": album.get("artist", ""),
            "artist_id": album.get("artist_id", ""),
            "guests": [g for g in (album.get("guests") or []) if isinstance(g, dict)],
            "festival_id": album.get("festival_id") or (
                entities.festival_slug(album["festival"]) if album.get("festival") else ""),
            "entities": entities.album_entities(album),
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
            "imported_by": meta.get("imported_by", ""),
            "imported_at": meta.get("imported_at", ""),
            "drive_added_at": meta.get("drive_added_at", ""),
            "published": published,
        })

    # Tri
    def _sort_key(a: dict):
        if sort == "date_concert":
            raw = str(a.get("date") or "")
            # Formats : "2024", "2024-06", "2024-06-21", "21 mars 2026"
            # On extrait juste l'année pour les dates textuelles (fallback)
            try:
                parts = raw.split("-")
                return (parts[0].zfill(4), parts[1].zfill(2) if len(parts) > 1 else "00",
                        parts[2].zfill(2) if len(parts) > 2 else "00")
            except Exception:
                return ("0000", "00", "00")
        elif sort == "date_import":
            return a.get("imported_at", "") or ""
        elif sort == "artist":
            return (a.get("artist", "") or "").lower()
        elif sort == "title":
            return (a.get("title", "") or "").lower()
        return ""

    reverse = sort in ("date_concert", "date_import")  # plus récent en premier
    albums.sort(key=_sort_key, reverse=reverse)
    return albums


def _labels(album: dict, has_mp3: bool, has_mp4: bool) -> list[str]:
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
    seen, out = set(), []
    for l in labels:
        k = l.lower()
        if k not in seen:
            seen.add(k)
            out.append(l)
    return out


def all_labels() -> list[str]:
    s: set[str] = set()
    for a in list_albums():
        s.update(a.get("labels", []))
    return sorted(s, key=str.lower)
