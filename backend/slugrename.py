"""Correction/renommage de l'URL (slug) d'un album.

Le slug est l'identifiant de l'album : nom du dossier projet ET clé des données
sociales (favoris, commentaires, pochettes, pochettes par piste, notifications,
annonces). Le renommer implique donc trois choses menées ensemble :

1. renommer le dossier `PROJECTS_DIR/<slug>` (les fichiers cover/audio y vivent,
   ils suivent automatiquement) ;
2. migrer les colonnes `slug` des tables sociales de l'environnement courant ;
3. enregistrer un alias `ancien → nouveau` pour rediriger (301) les URLs déjà
   partagées.

Choix de conception : renommage **manuel** (action « Corriger l'URL »), jamais
automatique à chaque édition — une URL est un identifiant stable. Voir la
décision produit associée dans DECISIONS.md.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import datetime, timezone

from . import db
from .db import get_conn
from .manifest import PROJECTS_DIR, Manifest, slugify

_ALIAS_DDL = (
    "CREATE TABLE IF NOT EXISTS slug_aliases ("
    "old_slug TEXT PRIMARY KEY, new_slug TEXT NOT NULL, created_at TEXT NOT NULL)"
)

# Colonnes `slug` à réécrire (toutes de simples UPDATE ; les *_likes cascadent
# via leur FK cover_id/comment_id, donc rien à faire de ce côté).
_SLUG_COLUMNS = [
    ("favorites", "slug"),
    ("comments", "slug"),
    ("covers", "slug"),
    ("track_covers", "slug"),
    ("notifications", "slug"),
    ("post_announcements", "slug"),  # slug = PRIMARY KEY, mais le nouveau slug
]                                     # n'a jamais été annoncé → pas de conflit.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_slug(album: dict) -> str:
    """Slug canonique « propre » d'un album : `artiste-date` si la date est
    connue (format ISO), sinon `artiste-titre`, sinon `artiste`.

    Volontairement au format `artiste-date` (cohérent avec l'existant et court
    pour une URL), distinct du nom de fichier ZIP `date_artiste_titre`.
    """
    artist = slugify(str(album.get("artist", "") or ""))
    if not artist or artist == "untitled":
        artist = ""
    date = str(album.get("date", "") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        base = f"{artist}-{date}" if artist else date
    else:
        title = slugify(str(album.get("title", "") or ""))
        if title and title != "untitled":
            base = f"{artist}-{title}" if artist else title
        else:
            base = artist
    return base.strip("-")


def unique_slug(base: str, current: str) -> str:
    """`base`, suffixé `-2`, `-3`… si un autre dossier projet l'occupe déjà.
    `current` (le slug actuel de l'album) n'est pas considéré comme un conflit.
    """
    cand = base
    i = 2
    while cand != current and (PROJECTS_DIR / cand).exists():
        cand = f"{base}-{i}"
        i += 1
    return cand


def canonical_slug(slug: str) -> str:
    """Suit la chaîne d'alias jusqu'au slug actuel. Renvoie `slug` inchangé
    s'il n'est pas un alias (ou en cas d'erreur base — la lecture reste sûre)."""
    try:
        with get_conn() as conn:
            seen = set()
            cur = slug
            while cur not in seen:
                seen.add(cur)
                row = conn.execute(
                    "SELECT new_slug FROM slug_aliases WHERE old_slug=?", (cur,)
                ).fetchone()
                if not row:
                    break
                cur = row["new_slug"]
            return cur
    except sqlite3.Error:
        return slug


def _social_db_paths() -> list[Path]:
    """Toutes les bases sociales à migrer.

    Le dossier `projects/` est partagé entre environnements (bind-mount) mais
    chaque env a SA base sous `.l2m-social/<env>/`. Un renommage touche le
    dossier partagé : il faut donc réécrire le slug dans **toutes** les bases,
    sinon l'autre env garde des lignes sociales orphelines. On inclut la base
    de l'env courant (chemin éventuellement surchargé par `L2M_DATA_DIR`).
    """
    paths = {db.DB_PATH.resolve()} if db.DB_PATH.exists() else set()
    root = PROJECTS_DIR / ".l2m-social"
    if root.exists():
        for p in root.glob("*/live2mp3.db"):
            paths.add(p.resolve())
    return sorted(paths)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _migrate_one(conn: sqlite3.Connection, old: str, new: str) -> None:
    conn.execute(_ALIAS_DDL)
    # Ne touche que les tables présentes : les bases d'env peuvent différer
    # légèrement (drift de schéma / migration non encore jouée).
    for table, col in _SLUG_COLUMNS:
        if _table_exists(conn, table):
            conn.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (new, old))
    # Compression de chaîne : les alias qui pointaient vers `old` pointent
    # désormais vers `new`, puis on enregistre `old → new`.
    conn.execute("UPDATE slug_aliases SET new_slug=? WHERE new_slug=?", (new, old))
    conn.execute(
        "INSERT OR REPLACE INTO slug_aliases(old_slug, new_slug, created_at) "
        "VALUES(?,?,?)",
        (old, new, _now()),
    )
    # Un ancien alias identique au nouveau slug n'a plus de sens (auto-référence).
    conn.execute("DELETE FROM slug_aliases WHERE old_slug=new_slug")


def _migrate_all_dbs(old: str, new: str) -> None:
    for path in _social_db_paths():
        conn = sqlite3.connect(str(path), timeout=10.0)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            _migrate_one(conn, old, new)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def rename_album(old_slug: str) -> dict:
    """Corrige l'URL d'un album vers son slug canonique.

    Renvoie {"changed": bool, "slug": <slug final>, "reason": <code si inchangé>}.
    `reason` ∈ {"already_clean", "no_metadata"} quand rien ne change.
    """
    src = PROJECTS_DIR / old_slug
    manifest_path = src / "manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError("album introuvable")
    m = Manifest.load(manifest_path)
    base = clean_slug(m.data.get("album", {}))
    if not base:
        return {"changed": False, "slug": old_slug, "reason": "no_metadata"}
    target = unique_slug(base, old_slug)
    if target == old_slug:
        return {"changed": False, "slug": old_slug, "reason": "already_clean"}

    dst = PROJECTS_DIR / target
    # 1) Dossier d'abord : si ça échoue (droits…), on n'a rien touché en base.
    shutil.move(str(src), str(dst))
    # 2) Bases (tous les env) : en cas d'échec, on remet le dossier en place.
    try:
        _migrate_all_dbs(old_slug, target)
    except Exception:
        shutil.move(str(dst), str(src))
        raise
    return {"changed": True, "slug": target, "reason": ""}
