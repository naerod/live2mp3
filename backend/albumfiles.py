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


def _norm_title(s: str) -> str:
    """Normalise pour comparer titres et noms de fichiers (minuscules, sans
    ponctuation, espaces compactés)."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def map_files_to_tracks(audio_dir: Path, tracks: list) -> list[tuple[int, dict, Path | None]]:
    """Associe chaque piste du manifest à son fichier MP3.

    Priorité au **titre** (présent dans quasiment tous les noms de fichiers,
    quel que soit leur préfixe : `02.`, `[SPOTDOWNLOADER.COM]`, artiste…), avec
    repli sur le préfixe numérique pour les albums rendus par le pipeline. Un
    fichier n'est associé qu'une fois. Retourne [(position 1-indexée, piste,
    fichier|None), …] dans l'ordre du manifest.
    """
    files = sorted(audio_dir.glob("*.mp3")) if audio_dir.exists() else []
    used: set[Path] = set()
    result: list[list] = []
    # Passe 1 : appariement par titre.
    for i, t in enumerate(tracks):
        nt = _norm_title(t.get("title", ""))
        chosen = None
        if nt:
            for f in files:
                if f not in used and nt in _norm_title(f.stem):
                    chosen = f
                    used.add(f)
                    break
        result.append([i + 1, t, chosen])
    # Passe 2 : repli sur le préfixe numérique pour les pistes non appariées.
    for row in result:
        if row[2] is not None:
            continue
        n = int(row[1].get("n", -1))
        for f in files:
            if f not in used and _file_track_n(f.stem) == n:
                row[2] = f
                used.add(f)
                break
    return [(pos, t, f) for pos, t, f in result]


def _rename_audio_files(slug: str, m: Manifest) -> int:
    """Renomme les MP3 au format '01. Titre.mp3' en suivant l'ordre du manifest."""
    audio_dir = _projects_dir() / slug / "build" / "audio"
    if not audio_dir.exists():
        return 0
    # Passe 1 : renommer vers un nom temporaire pour éviter les collisions
    pending: dict[Path, Path] = {}
    for new_pos, t, mp3_path in map_files_to_tracks(audio_dir, m.tracks):
        if mp3_path is None:
            continue
        safe_title = _sanitize_filename(t.get("title", "") or f"Track {t.get('n')}")
        new_name = f"{new_pos:02d}. {safe_title}.mp3"
        if mp3_path.name == new_name:
            continue
        tmp_path = mp3_path.parent / f"._tmp_{new_pos}_{mp3_path.name}"
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
            # Pas de notion de disque dans live2mp3 : un TPOS hétérogène hérité
            # des fichiers source scinde l'album en « Disc 1 / Disc 2 ».
            tags.pop("discnumber", None)
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
    total = len(m.tracks)
    tagged = 0
    for pos, t, mp3_path in map_files_to_tracks(audio_dir, m.tracks):
        if mp3_path is None:
            continue
        try:
            try:
                tags = EasyID3(str(mp3_path))
            except ID3NoHeaderError:
                tags = EasyID3()
                tags.save(str(mp3_path))
                tags = EasyID3(str(mp3_path))
            # Titre NU dans le tag : c'est ce qu'affiche Finamp, qui numérote
            # déjà ses lignes (cf. manifest.numbered_title). L'ordre est porté
            # par TRCK juste en dessous, et par le nom de fichier numéroté.
            tags["title"] = [t.get("title", "")]
            tags["tracknumber"] = [f"{pos}/{total}"]
            tags.save()
            tagged += 1
        except Exception:
            pass
    return tagged


def _write_album_cover(slug: str, m: Manifest) -> int:
    """Embarque la pochette du site (album.cover) comme APIC dans tous les MP3."""
    audio_dir = _projects_dir() / slug / "build" / "audio"
    if not audio_dir.exists():
        return 0
    cover_rel = m.data.get("album", {}).get("cover")
    if not cover_rel:
        # Plus de pochette d'album : retirer l'APIC de toutes les pistes pour
        # que Finamp/Jellyfin cessent d'afficher une pochette embarquée obsolète.
        cleared = 0
        _write_disc_cover(slug, m)      # retire aussi le disc.* devenu orphelin
        for mp3_path in audio_dir.glob("*.mp3"):
            try:
                tags = ID3(str(mp3_path))
                if tags.getall("APIC"):
                    tags.delall("APIC")
                    tags.save(str(mp3_path))
                    cleared += 1
            except Exception:
                pass
        return cleared
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
    # Même pochette côté serveur média : image de dossier (Primary) + Disc.
    try:
        (audio_dir / "cover.jpg").write_bytes(data)
    except OSError:
        pass
    _write_disc_cover(slug, m)
    return embedded


def _write_disc_cover(slug: str, m: Manifest) -> bool:
    """Dépose la pochette 1:1 comme image « Disc » Jellyfin (`disc.jpg`).

    Jellyfin reconnaît, dans le dossier d'un album, `cover.jpg`/`folder.jpg`
    comme image **Primary** et `disc.<ext>` comme image **Disc** (l'art du
    disque, carré lui aussi). On y recopie donc la pochette gagnante : la
    vignette carrée du site devient la pochette « disque » du serveur média,
    sans intervention manuelle et **sans passer par l'API** — un fichier est
    re-détecté à chaque scan, alors qu'une image téléversée par l'API est
    effacée par un rafraîchissement `ReplaceAllImages`.

    Sans pochette, les `disc.*` existants sont retirés (pas d'image fantôme).
    """
    audio_dir = _projects_dir() / slug / "build" / "audio"
    if not audio_dir.exists():
        return False
    cover_rel = m.data.get("album", {}).get("cover")
    cover_path = _projects_dir() / slug / cover_rel if cover_rel else None
    try:
        for old in audio_dir.glob("disc.*"):
            old.unlink(missing_ok=True)
        if not cover_path or not cover_path.exists():
            return False
        ext = cover_path.suffix.lower() if cover_path.suffix.lower() in COVER_MIME else ".jpg"
        (audio_dir / f"disc{ext}").write_bytes(cover_path.read_bytes())
        return True
    except OSError:
        return False


def _write_folder_cover(slug: str, m: Manifest) -> bool:
    """Dépose `album.cover` comme image de dossier `build/audio/cover.jpg`.

    Sert la vignette d'album à Jellyfin **sans toucher aux APIC des pistes** :
    indispensable aux albums à pochette par piste (`per_track_covers`), où
    Jellyfin déduirait sinon la vignette de la 1ʳᵉ piste. Jellyfin préfère un
    `cover.jpg`/`folder.jpg` présent dans le dossier à l'art embarqué.
    """
    audio_dir = _projects_dir() / slug / "build" / "audio"
    cover_rel = m.data.get("album", {}).get("cover")
    if not audio_dir.exists() or not cover_rel:
        return False
    cover_path = _projects_dir() / slug / cover_rel
    if not cover_path.exists():
        return False
    try:
        (audio_dir / "cover.jpg").write_bytes(cover_path.read_bytes())
    except OSError:
        return False
    _write_disc_cover(slug, m)
    return True


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
        # Rattrapage automatique : repointage technique, pas une modification
        # editoriale de l'album (cf. incident 2026-09-21).
        m.save(touch=False)
        return f"artwork/cover{ext}"
    return None
