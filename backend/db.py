"""Base SQLite pour les données sociales (profils, favoris, commentaires).

Les manifests YAML restent la source de vérité des *albums*. SQLite ne stocke
que ce qui est propre à l'utilisateur et concurrent (favoris, commentaires,
votes, profils) — inadapté à une réécriture de fichier à chaque action.

Chemin des données pilotable par `L2M_DATA_DIR` (monté sur un volume Docker
dédié, séparé par environnement prod/preprod). WAL activé pour supporter les
lectures/écritures concurrentes du serveur multi-thread.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_BASE = Path(__file__).resolve().parent.parent
# Par défaut, on stocke sous le volume `projects` déjà monté (bind-mount partagé
# sur les hôtes), dans un sous-dossier caché scoppé par environnement — évite
# toute modification de docker-compose.yml. `.l2m-social/` est ignoré par le
# scan du catalogue (aucun manifest.yaml). Surchargeable via `L2M_DATA_DIR`.
_APP_ENV = os.environ.get("APP_ENV", "prod")
_DEFAULT_DIR = _BASE / "projects" / ".l2m-social" / _APP_ENV
DATA_DIR = Path(os.environ.get("L2M_DATA_DIR", str(_DEFAULT_DIR)))
AVATARS_DIR = DATA_DIR / "avatars"
DB_PATH = DATA_DIR / "live2mp3.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    username     TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    bio          TEXT NOT NULL DEFAULT '',
    avatar_ext   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS favorites (
    username   TEXT NOT NULL,
    slug       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (username, slug)
);
CREATE INDEX IF NOT EXISTS idx_favorites_slug ON favorites(slug);

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slug       TEXT NOT NULL,
    parent_id  INTEGER REFERENCES comments(id) ON DELETE CASCADE,
    reply_to   TEXT NOT NULL DEFAULT '',
    username   TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    edited_at  TEXT NOT NULL DEFAULT '',
    deleted    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_comments_slug   ON comments(slug);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_user   ON comments(username);

CREATE TABLE IF NOT EXISTS comment_likes (
    comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
    username   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (comment_id, username)
);
CREATE INDEX IF NOT EXISTS idx_likes_comment ON comment_likes(comment_id);
"""


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Connexion transactionnelle : commit si succès, rollback si exception."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    AVATARS_DIR.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Migration : comment_votes (upvote/downvote) → comment_likes
        if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='comment_votes'"
        ).fetchone():
            conn.execute(
                "INSERT OR IGNORE INTO comment_likes(comment_id, username, created_at) "
                "SELECT comment_id, username, datetime('now') FROM comment_votes WHERE value=1"
            )
            conn.execute("DROP TABLE comment_votes")
