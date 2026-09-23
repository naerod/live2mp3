"""Catalogue des albums live traités — alimente la vitrine publique.

Scanne `projects/` : chaque dossier avec un manifest et des rendus disponibles
devient une entrée (métadonnées + disponibilité MP3/MP4 + cover + import info).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from . import entities
from .db import get_conn
from .manifest import Manifest, PROJECTS_DIR


APP_ENV = os.environ.get("APP_ENV", "prod")


def hidden_by_env(data: dict) -> bool:
    """Album créé en preprod : invisible depuis la prod tant qu'il n'est pas poussé.

    Le stockage des albums est physiquement **partagé** entre prod et preprod
    (même bind-mount, cf. `workspace/infra/stockage.md`). Sans ce filtre, le
    moindre essai fait en preprod apparaît aussitôt dans le catalogue et les
    brouillons de la production.

    Champ absent = album de production : les albums existants restent visibles
    sans aucune migration. La preprod, elle, voit tout — c'est son rôle.
    """
    origin = data.get("origin_env")
    return APP_ENV == "prod" and bool(origin) and origin != "prod"


def _has_files(d: Path, ext: str) -> bool:
    return d.exists() and any(d.glob(f"*.{ext}"))


def _cover_slugs() -> set[str]:
    """Slugs ayant au moins une pochette en base (système social).

    Même logique que _traycard_slugs : requête unique pour éviter le N+1.
    En cas d'erreur, on retombe sur le manifest — la vitrine reste lisible.
    """
    try:
        with get_conn() as conn:
            return {
                r["slug"]
                for r in conn.execute("SELECT DISTINCT slug FROM covers")
            }
    except Exception:
        return set()


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


def album_status(published: bool, has_mp3: bool, has_mp4: bool,
                 has_video_full: bool) -> str:
    """Statut de cycle de vie **unique et prioritaire** d'un album.

    Un seul état dominant, du plus urgent au plus abouti — c'est le code rendu
    en badge côté front (libellé + icône + couleur y sont mappés) :
      - `draft`            : brouillon (aucun média rendu) — géré par list_drafts.
      - `unpublished`      : média présent mais caché du public.
      - `published_audio`  : public, audio seul (le MP4 manque).
      - `published_av`     : public, audio + vidéo (concert complet ou clips).
    """
    if not published:
        return "unpublished"
    if has_mp4 or has_video_full:
        return "published_av"
    return "published_audio"


def stamp_first_published(m) -> bool:
    """Horodate la **première** publication d'un album, et une seule fois.

    `meta.first_published_at` est la date de mise en ligne originelle : c'est
    elle qui pilote le tri « Nouveauté » et le badge « Nouveau », pas la date
    d'import (un album importé il y a longtemps puis publié aujourd'hui est une
    nouveauté pour le public). Une dépublication suivie d'une republication ne
    la réécrit pas : l'album ne redevient jamais « nouveau ».

    Renvoie True si le manifest a été modifié (appelant responsable du save).
    """
    meta = m.data.setdefault("meta", {})
    if meta.get("first_published_at"):
        return False
    meta["first_published_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return True


def backfill_first_published() -> int:
    """Renseigne `first_published_at` / `updated_at` sur les albums antérieurs.

    **Ne jamais dériver ces dates du mtime du manifest** (bug 2026-09-21) : le
    mtime est réécrit par n'importe quelle écriture technique — y compris par
    ce backfill lui-même, qui tournait à chaque démarrage. Des albums importés
    depuis des mois héritaient ainsi d'une « première publication » égale à la
    date d'un redémarrage récent, et s'affichaient « Nouveau » puis
    « Mis à jour » par lots entiers (horodatages identiques à la seconde).

    Repli retenu : `imported_at`, la seule date réelle dont on dispose. Sans
    `imported_at`, on n'écrit rien — mieux vaut pas de badge qu'un faux badge.
    Le passage est marqué (`meta.backfilled`) pour être définitivement unique :
    plus aucune réécriture au démarrage, donc plus de churn de mtime.
    """
    if not PROJECTS_DIR.exists():
        return 0
    done = 0
    for pdir in sorted(PROJECTS_DIR.iterdir()):
        path = pdir / "manifest.yaml"
        if not path.is_file():
            continue
        try:
            m = Manifest.load(path)
        except Exception:
            continue
        meta = m.data.setdefault("meta", {})
        if meta.get("backfilled"):
            continue
        if meta.get("first_published_at") and meta.get("updated_at"):
            meta["backfilled"] = True
        else:
            imported = str(meta.get("imported_at") or "")
            if not imported:
                continue  # aucune date fiable : on n'invente pas
            if m.data.get("published", True) and not meta.get("first_published_at"):
                meta["first_published_at"] = imported
            if not meta.get("updated_at"):
                meta["updated_at"] = meta.get("first_published_at") or imported
            meta["backfilled"] = True
        try:
            m.save(path, touch=False)
            done += 1
        except Exception:
            continue
    return done


def list_albums(sort: str = "date_concert", include_drafts: bool = False) -> list[dict]:
    albums: list[dict] = []
    if not PROJECTS_DIR.exists():
        return albums
    cover_slugs = _cover_slugs()
    tray_slugs = _traycard_slugs()
    for pdir in sorted(PROJECTS_DIR.iterdir()):
        manifest = pdir / "manifest.yaml"
        if not manifest.is_file():
            continue
        try:
            m = Manifest.load(manifest)
        except Exception:
            continue
        if hidden_by_env(m.data):
            continue
        published = m.data.get("published", True)
        if not published and not include_drafts:
            continue
        album = m.data.get("album", {})
        has_mp3 = _has_files(pdir / "build" / "audio", "mp3")
        # Concert complet (MP4 unique), dans build/video-full. `build/video` est
        # l'ancien dossier (clips par piste, puis rendus égarés du 2026-08-13 au
        # 2026-09-18) : encore lu pour ne pas faire disparaître un album tant
        # qu'il n'a pas été migré.
        has_video_full = _has_files(pdir / "build" / "video-full", "mp4")
        has_mp4 = has_video_full or _has_files(pdir / "build" / "video", "mp4")
        if not (has_mp3 or has_mp4 or has_video_full):
            continue
        cover_rel = album.get("cover")
        # Priorité à la DB sociale ; fallback sur le manifest pour les covers
        # legacy ou uploadées directement (hors système social).
        has_cover = (pdir.name in cover_slugs) or bool(
            cover_rel and (pdir / cover_rel).exists()
        )
        # Version de la pochette servie par /cover/{slug} : mtime en secondes,
        # utilisé comme query param côté front pour invalider le cache navigateur
        # dès qu'un gestionnaire remplace la cover (sinon la vitrine affiche
        # l'ancienne image tant que le fichier a le même nom).
        cover_v = 0
        if cover_rel:
            try:
                cover_v = int((pdir / cover_rel).stat().st_mtime)
            except OSError:
                cover_v = 0
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
            "city": album.get("city", ""),
            "festival": album.get("festival", ""),
            "tracks": len(m.tracks),
            "has_mp3": has_mp3,
            "has_mp4": has_mp4,
            "has_video_full": has_video_full,
            "has_cover": has_cover,
            "cover_v": cover_v,
            "has_traycard": has_traycard,
            "labels": _labels(album, has_mp3, has_mp4, has_video_full),
            "status": album_status(published, has_mp3, has_mp4, has_video_full),
            "imported_by": meta.get("imported_by", ""),
            "imported_at": meta.get("imported_at", ""),
            "drive_added_at": meta.get("drive_added_at", ""),
            "first_published_at": meta.get("first_published_at", ""),
            "updated_at": meta.get("updated_at", ""),
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
        elif sort in ("date_publication", "date_import"):
            # « Nouveauté » = date de mise en ligne, pas date d'import.
            # Repli sur l'import pour les brouillons / non publiés.
            return (a.get("first_published_at") or a.get("imported_at") or "")
        elif sort == "artist":
            return (a.get("artist", "") or "").lower()
        elif sort == "title":
            return (a.get("title", "") or "").lower()
        return ""

    reverse = sort in ("date_concert", "date_import", "date_publication")  # plus récent en premier
    albums.sort(key=_sort_key, reverse=reverse)
    return albums


def list_drafts() -> list[dict]:
    """Imports commencés mais jamais rendus — invisibles de la vitrine.

    `list_albums` écarte tout projet sans MP3 ni MP4 : un import interrompu
    avant l'étape de rendu n'apparaît donc nulle part dans l'UI, alors que son
    dossier occupe le slug (l'utilisateur se retrouve bloqué au réimport par un
    409 sans pouvoir reprendre ni supprimer). On liste ici exactement le
    complément : manifest présent, aucun média produit.

    L'étape atteinte vient de `pipeline_state` ; `updated_at` est le mtime du
    manifest (dernière action réelle sur le brouillon), pas la date d'import,
    pour que le badge d'ancienneté reflète l'abandon et non la création.
    """
    drafts: list[dict] = []
    if not PROJECTS_DIR.exists():
        return drafts
    for pdir in sorted(PROJECTS_DIR.iterdir()):
        manifest = pdir / "manifest.yaml"
        if not manifest.is_file():
            continue
        if _has_files(pdir / "build" / "audio", "mp3") or \
           _has_files(pdir / "build" / "video-full", "mp4") or \
           _has_files(pdir / "build" / "video", "mp4"):
            continue
        try:
            m = Manifest.load(manifest)
        except Exception:
            continue
        if hidden_by_env(m.data):
            continue
        album = m.data.get("album", {})
        meta = m.data.get("meta", {})
        state = m.data.get("pipeline_state", {}) or {}
        # Dernière étape terminée : repère de reprise affiché dans la liste.
        done = [s for s in ("download", "waveform", "ai_markers", "render",
                            "tags", "artwork", "disc") if state.get(s) == "done"]
        try:
            updated_at = datetime.fromtimestamp(
                manifest.stat().st_mtime, tz=timezone.utc
            ).isoformat(timespec="seconds")
        except OSError:
            updated_at = ""
        drafts.append({
            "slug": pdir.name,
            "artist": album.get("artist", ""),
            "title": album.get("title", ""),
            "date": album.get("date", ""),
            "venue": album.get("venue", ""),
            "tracks": len(m.tracks),
            "imported_by": meta.get("imported_by", ""),
            "imported_at": meta.get("imported_at", ""),
            "updated_at": updated_at,
            "stage": done[-1] if done else "",
            "status": "draft",
        })
    drafts.sort(key=lambda d: d.get("updated_at", ""), reverse=True)
    return drafts


def _labels(album: dict, has_mp3: bool, has_mp4: bool,
            has_video_full: bool = False) -> list[str]:
    """Libellés d'un album, précédés du média réellement disponible.

    `has_video_full` (concert complet en MP4 unique) compte comme de la vidéo
    au même titre que les clips par piste : sans ça un album MP3 + concert
    complet s'affichait « audio » juste sous un badge « Publié (MP3 + MP4) ».
    """
    labels = list(album.get("labels", []) or [])
    video = has_mp4 or has_video_full
    if has_mp3 and video:
        media = "audio + vidéo"
    elif video:
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
