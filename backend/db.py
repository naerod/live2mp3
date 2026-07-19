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
-- Personnalisation : `*_id` est la clé canonique (code INSEE, id Deezer) et
-- `*_label` le rendu figé au moment du choix. On garde les deux : l'id permet
-- de regrouper (même ville, même artiste) sans se fier au texte, le label
-- permet d'afficher un profil sans dépendre de la disponibilité de la source.
CREATE TABLE IF NOT EXISTS profiles (
    username     TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    bio          TEXT NOT NULL DEFAULT '',
    avatar_ext   TEXT NOT NULL DEFAULT '',
    city_id      TEXT NOT NULL DEFAULT '',
    city_label   TEXT NOT NULL DEFAULT '',
    artist_id    TEXT NOT NULL DEFAULT '',
    artist_label TEXT NOT NULL DEFAULT '',
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

-- Suivis (« follow » façon X/YouTube). Une ligne = un utilisateur suit une
-- entité. `target_type` ∈ {artist, festival, venue, user}. `target_id` est la
-- clé canonique de l'entité (id Deezer, slug festival/lieu, ou username).
-- `target_label` est le libellé figé au moment du suivi — pour afficher la
-- liste « Abonnements » sans re-solliciter une source tierce.
-- `notify` = la cloche : 1 = notifications actives (défaut au suivi), 0 = suivi
-- mais en sourdine. Le suivi seul alimentera le futur feed ; la cloche pilote
-- les notifications.
CREATE TABLE IF NOT EXISTS follows (
    username     TEXT NOT NULL,
    target_type  TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    target_label TEXT NOT NULL DEFAULT '',
    notify       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (username, target_type, target_id)
);
CREATE INDEX IF NOT EXISTS idx_follows_user   ON follows(username);
CREATE INDEX IF NOT EXISTS idx_follows_target ON follows(target_type, target_id);

-- Notifications in-app. `type` = catégorie ('new_post' pour l'instant).
-- `reason_*` = l'entité suivie qui a déclenché la notif (pour le libellé
-- « Nouveau post de <X> que vous suivez »). `slug`/`title`/`subtitle` = la
-- cible (le post), libellés figés à l'émission. `read` = lu/non-lu.
CREATE TABLE IF NOT EXISTS notifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT NOT NULL,
    type         TEXT NOT NULL,
    actor        TEXT NOT NULL DEFAULT '',
    reason_type  TEXT NOT NULL DEFAULT '',
    reason_id    TEXT NOT NULL DEFAULT '',
    reason_label TEXT NOT NULL DEFAULT '',
    slug         TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    subtitle     TEXT NOT NULL DEFAULT '',
    read         INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(username, read);
CREATE INDEX IF NOT EXISTS idx_notif_recent ON notifications(username, created_at);

-- Préférences de notification par utilisateur et par catégorie. Une ligne
-- absente = tout activé (opt-out, conforme RGPD : l'utilisateur peut couper).
-- `inapp` pilote la cloche/centre ; `email` prépare la Phase 3 (envoi encore
-- désactivé par un master-switch, voir notifications.py).
CREATE TABLE IF NOT EXISTS notif_prefs (
    username  TEXT NOT NULL,
    pref_key  TEXT NOT NULL,
    inapp     INTEGER NOT NULL DEFAULT 1,
    email     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (username, pref_key)
);

-- Idempotence du fan-out : un post n'est annoncé qu'une fois par environnement
-- (les manifests sont partagés prod/preprod mais les notifs sont scindées).
-- Ligne sentinelle '__seeded__' = les posts déjà publiés avant la Phase 2 ont
-- été marqués « déjà annoncés » (pas de spam rétroactif).
CREATE TABLE IF NOT EXISTS post_announcements (
    slug         TEXT PRIMARY KEY,
    announced_at TEXT NOT NULL
);

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

-- Pochettes proposées par les utilisateurs. Plusieurs par album ; celle qui
-- s'affiche est résolue à la lecture (épinglée > plus likée), jamais stockée.
-- La tray card n'existe pas seule : c'est une colonne de la cover qui la porte
-- (`traycard_ext` vide = pas de tray card), ce qui rend le lien 1:1 impossible
-- à casser.
CREATE TABLE IF NOT EXISTS covers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT NOT NULL,
    username     TEXT NOT NULL,
    -- Nom du fichier sur disque, volontairement décorrélé de `id` : prod et
    -- preprod partagent le volume albums mais ont chacune leur base. Deux `id`
    -- autoincrémentés indépendants désigneraient le même fichier — la clé, elle,
    -- est identique des deux côtés ('legacy') ou globalement unique (uuid).
    file_key     TEXT NOT NULL,
    cover_ext    TEXT NOT NULL,
    traycard_ext TEXT NOT NULL DEFAULT '',
    caption      TEXT NOT NULL DEFAULT '',
    pinned       INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_covers_slug ON covers(slug);
CREATE INDEX IF NOT EXISTS idx_covers_user ON covers(username);
-- Rejoue la migration legacy sans créer de doublon (file_key='legacy').
CREATE UNIQUE INDEX IF NOT EXISTS idx_covers_file ON covers(slug, file_key);
-- Au plus une cover épinglée par album : index partiel, seules les lignes
-- pinned=1 y entrent, donc l'unicité ne contraint qu'elles.
CREATE UNIQUE INDEX IF NOT EXISTS idx_covers_pinned
    ON covers(slug) WHERE pinned = 1;

CREATE TABLE IF NOT EXISTS cover_likes (
    cover_id   INTEGER NOT NULL REFERENCES covers(id) ON DELETE CASCADE,
    username   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (cover_id, username)
);
CREATE INDEX IF NOT EXISTS idx_cover_likes_cover ON cover_likes(cover_id);
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

        # Migration : commentaires portés par une cover. NULL = commentaire
        # d'album (comportement historique), sinon fil de la cover visée.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(comments)")}
        if "cover_id" not in cols:
            conn.execute(
                "ALTER TABLE comments ADD COLUMN cover_id INTEGER "
                "REFERENCES covers(id) ON DELETE CASCADE"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comments_cover ON comments(cover_id)"
        )

        # Migration : personnalisation du profil (ville, artiste favori).
        pcols = {r["name"] for r in conn.execute("PRAGMA table_info(profiles)")}
        for col in ("city_id", "city_label", "artist_id", "artist_label"):
            if col not in pcols:
                conn.execute(
                    f"ALTER TABLE profiles ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
        # Migration : rôle en cache (utilisateur/gestionnaire/admin). Renseigné
        # à chaque passage authentifié depuis les groupes Authentik (voir
        # social.social_me) — permet d'afficher un badge de rôle sur n'importe
        # quel profil sans dépendre de l'API Authentik.
        if "role" not in pcols:
            conn.execute(
                "ALTER TABLE profiles ADD COLUMN role TEXT NOT NULL DEFAULT ''"
            )
        # Regroupements « même ville » / « même artiste » sur la clé canonique.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_profiles_city ON profiles(city_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_profiles_artist ON profiles(artist_id)"
        )
        _migrate_track_covers(conn)





def _migrate_track_covers(conn) -> None:
    if conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='track_covers'"
    ).fetchone():
        return
    conn.executescript(
        "CREATE TABLE track_covers ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "slug TEXT NOT NULL,"
        "track_n INTEGER NOT NULL,"
        "username TEXT NOT NULL,"
        "file_key TEXT NOT NULL,"
        "cover_ext TEXT NOT NULL,"
        "created_at TEXT NOT NULL,"
        "updated_at TEXT NOT NULL,"
        "UNIQUE(slug, track_n));"
        "CREATE INDEX idx_tc_slug ON track_covers(slug);"
    )
