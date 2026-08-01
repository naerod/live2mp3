"""Notifications in-app + préférences (Phase 2) et préparation email (Phase 3).

Un post publié (fiche album) est *annoncé* à ses abonnés : les followers des
entités liées (artiste principal, invités, festival, lieu) et de l'auteur, qui
ont la cloche active (`follows.notify=1`) et n'ont pas coupé la catégorie
correspondante dans leurs préférences.

Idempotence : chaque post n'est annoncé qu'une fois **par environnement**
(table `post_announcements`), les manifests étant partagés prod/preprod.

L'email (Phase 3) est déjà modélisé (colonne `email` des préférences, footer de
désabonnement prévu) mais **jamais envoyé** tant que le master-switch
`NOTIFY_EMAIL_ENABLED` est faux — désactivé sur la preprod.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import entities
from .auth import current_identity, require_user
from .db import get_conn
from .manifest import PROJECTS_DIR, Manifest

router = APIRouter()

BASE = Path(__file__).resolve().parent.parent
FRONTEND = BASE / "frontend"

# Catégories de notification (clé de préférence), regroupées pour l'UI en trois
# rubriques : nouveaux posts, likes, commentaires. Chaque clé pilote un canal
# in-app (et email en Phase 3).
NOTIF_NEW_POST = ["new_post:artist", "new_post:festival",
                  "new_post:venue", "new_post:user"]
NOTIF_LIKE = ["like:post", "like:comment"]
NOTIF_COMMENT = ["comment:post", "comment:reply"]
NOTIF_CATEGORIES = NOTIF_NEW_POST + NOTIF_LIKE + NOTIF_COMMENT

# Regroupement présenté dans /settings (l'ordre fait foi côté UI).
NOTIF_GROUPS = [
    {"key": "new_posts", "keys": NOTIF_NEW_POST},
    {"key": "likes", "keys": NOTIF_LIKE},
    {"key": "comments", "keys": NOTIF_COMMENT},
]

# Master-switch d'envoi email (Phase 3). Faux par défaut → aucun email en
# preprod ; l'utilisateur l'activera en prod une fois le relais d'envoi choisi.
EMAIL_ENABLED = os.environ.get("NOTIFY_EMAIL_ENABLED", "false").lower() in ("1", "true", "yes")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# =====================================================================
#  PRÉFÉRENCES
# =====================================================================
def _prefs_map(conn, username: str) -> dict[str, dict]:
    """{pref_key: {inapp, email}} pour toutes les catégories, défauts remplis."""
    rows = {
        r["pref_key"]: r for r in conn.execute(
            "SELECT pref_key, inapp, email FROM notif_prefs WHERE username=?",
            (username,),
        ).fetchall()
    }
    out: dict[str, dict] = {}
    for key in NOTIF_CATEGORIES:
        r = rows.get(key)
        out[key] = {
            "inapp": bool(r["inapp"]) if r is not None else True,
            "email": bool(r["email"]) if r is not None else True,
        }
    return out


def _pref_allows(conn, username: str, pref_key: str, channel: str) -> bool:
    row = conn.execute(
        "SELECT inapp, email FROM notif_prefs WHERE username=? AND pref_key=?",
        (username, pref_key),
    ).fetchone()
    if row is None:
        return True   # défaut opt-out : tout activé
    return bool(row[channel])


class PrefIn(BaseModel):
    inapp: bool
    email: bool


class PrefsIn(BaseModel):
    prefs: dict[str, PrefIn]


@router.get("/api/social/notif-prefs")
def get_prefs(identity: dict = Depends(require_user)) -> dict:
    with get_conn() as conn:
        prefs = _prefs_map(conn, identity["username"])
    return {"categories": NOTIF_CATEGORIES, "groups": NOTIF_GROUPS, "prefs": prefs,
            "email_enabled": EMAIL_ENABLED}


@router.put("/api/social/notif-prefs")
def put_prefs(payload: PrefsIn, identity: dict = Depends(require_user)) -> dict:
    username = identity["username"]
    with get_conn() as conn:
        for key, val in payload.prefs.items():
            if key not in NOTIF_CATEGORIES:
                continue
            conn.execute(
                "INSERT INTO notif_prefs(username, pref_key, inapp, email) "
                "VALUES(?,?,?,?) ON CONFLICT(username, pref_key) DO UPDATE SET "
                "inapp=excluded.inapp, email=excluded.email",
                (username, key, 1 if val.inapp else 0, 1 if val.email else 0),
            )
        prefs = _prefs_map(conn, username)
    return {"ok": True, "prefs": prefs}


# =====================================================================
#  FAN-OUT À LA PUBLICATION
# =====================================================================
def _display_name(conn, username: str) -> str:
    row = conn.execute(
        "SELECT display_name FROM profiles WHERE username=?", (username,)
    ).fetchone()
    return (row["display_name"] if row and row["display_name"] else username)


def album_publisher_title(slug: str) -> tuple[str, str, str]:
    """(auteur, titre, artiste) d'un album. ('', '', '') si introuvable."""
    mpath = PROJECTS_DIR / slug / "manifest.yaml"
    if not mpath.is_file():
        return "", "", ""
    try:
        m = Manifest.load(mpath)
    except Exception:
        return "", "", ""
    alb = m.data.get("album", {})
    pub = (m.data.get("meta", {}) or {}).get("imported_by", "") or ""
    return pub, alb.get("title", slug), alb.get("artist", "")


def notify(conn, *, recipient: str, actor: str, ntype: str, pref_key: str,
           slug: str = "", title: str = "", subtitle: str = "") -> bool:
    """Crée une notification in-app pour une interaction (like/commentaire).

    Ne notifie jamais l'acteur lui-même, ni si le destinataire a coupé la
    catégorie. `reason_label` porte le nom d'affichage de l'acteur (le front
    l'utilise pour « X a aimé votre… »). Retourne True si une notif est créée.
    """
    if not recipient or recipient == actor:
        return False
    if not _pref_allows(conn, recipient, pref_key, "inapp"):
        return False
    conn.execute(
        "INSERT INTO notifications(username, type, actor, reason_type, reason_id, "
        "reason_label, slug, title, subtitle, read, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,0,?)",
        (recipient, ntype, actor, "", "", _display_name(conn, actor),
         slug, title, subtitle, _now()),
    )
    return True


def notify_role(conn, *, recipient: str, actor: str, new_role: str,
                promoted: bool) -> bool:
    """Notifie une action administrateur sur le rôle (promotion/rétrogradation).

    **Non désactivable** : aucune vérification de préférence (contrairement à
    `notify`) — une décision d'admin doit toujours être portée à la connaissance
    de l'intéressé. `type` = `role_grant` (promotion, habillage valorisant) /
    `role_revoke` (rétrogradation) ; `reason_type=admin` (badge « Action
    administrateur ») ; `reason_id` porte le nouveau rôle (le front en déduit le
    libellé) ; `reason_label` = nom de l'admin. Pas de `slug` : pointe vers le
    profil, pas un album."""
    conn.execute(
        "INSERT INTO notifications(username, type, actor, reason_type, reason_id, "
        "reason_label, slug, title, subtitle, read, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,0,?)",
        (recipient, "role_grant" if promoted else "role_revoke", actor, "admin",
         new_role, _display_name(conn, actor), "", "", "", _now()),
    )
    return True


def notify_render_done(slug: str, recipient: str) -> bool:
    """Notifie la fin d'un rendu à celui qui l'a lancé.

    **Non désactivable** : c'est l'aboutissement d'une action explicite de
    l'utilisateur, pas une sollicitation sociale. Pointe vers la fiche de
    gestion, puisque l'album sort du rendu non publié.
    """
    if not recipient:
        return False
    mpath = PROJECTS_DIR / slug / "manifest.yaml"
    album = {}
    if mpath.is_file():
        try:
            album = Manifest.load(mpath).data.get("album", {}) or {}
        except Exception:
            album = {}
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO notifications(username, type, actor, reason_type, "
            "reason_id, reason_label, slug, title, subtitle, read, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,0,?)",
            (recipient, "render_done", recipient, "render",
             album.get("artist_id", "") or "", album.get("artist", ""),
             slug, album.get("title", "") or slug,
             album.get("artist", ""), _now()),
        )
    return True


def render_activity(username: str) -> list[dict]:
    """Rendus en cours de l'utilisateur, en entrée « silencieuse ».

    Synthétiques comme les rappels de brouillon : listés en tête du panneau,
    jamais comptés dans la pastille et jamais marquables comme lus (cf.
    list_notifications). Un rendu en cours est un état, pas un évènement.
    """
    try:
        from . import renderqueue
        items = renderqueue.listing()
    except Exception:
        return []
    out: list[dict] = []
    for it in items:
        if it.get("requested_by") != username:
            continue
        mpath = PROJECTS_DIR / it["slug"] / "manifest.yaml"
        album = {}
        if mpath.is_file():
            try:
                album = Manifest.load(mpath).data.get("album", {}) or {}
            except Exception:
                album = {}
        out.append({
            "id": None,
            "type": "render_running",
            "username": username,
            "actor": username,
            "reason_type": "render",
            "reason_id": album.get("artist_id", "") or "",
            "reason_label": album.get("artist", ""),
            "slug": it["slug"],
            "title": album.get("title", "") or it["slug"],
            "subtitle": album.get("artist", ""),
            "state": it.get("state", "queued"),
            "formats": ["mp3", "mp4"] if it.get("video") else ["mp3"],
            "read": False,
            "created_at": it.get("queued_at") or time.time(),
        })
    return out


def announce_post(slug: str) -> int:
    """Annonce un post publié à ses abonnés. Idempotent par environnement.

    Retourne le nombre de notifications créées (0 si déjà annoncé, non publié,
    ou aucun abonné concerné).
    """
    mpath = PROJECTS_DIR / slug / "manifest.yaml"
    if not mpath.is_file():
        return 0
    try:
        m = Manifest.load(mpath)
    except Exception:
        return 0
    if not m.data.get("published", True):
        return 0
    album = m.data.get("album", {})
    publisher = (m.data.get("meta", {}) or {}).get("imported_by", "") or ""
    title = album.get("title", slug)
    subtitle = album.get("artist", "")

    # Cibles suivies : entités de l'album + l'auteur.
    targets = [(e["type"], e["id"], e["label"]) for e in entities.album_entities(album)]

    created = 0
    with get_conn() as conn:
        if conn.execute(
            "SELECT 1 FROM post_announcements WHERE slug=?", (slug,)
        ).fetchone():
            return 0
        if publisher:
            targets.append(("user", publisher, _display_name(conn, publisher)))

        # Un destinataire = une seule notif, même s'il suit plusieurs entités du
        # post. La 1re raison rencontrée (ordre des entités) l'emporte.
        recipients: dict[str, tuple[str, str, str]] = {}
        for ttype, tid, tlabel in targets:
            rows = conn.execute(
                "SELECT username FROM follows WHERE target_type=? AND target_id=? "
                "AND notify=1",
                (ttype, tid),
            ).fetchall()
            for r in rows:
                u = r["username"]
                if u == publisher or u in recipients:
                    continue
                recipients[u] = (ttype, tid, tlabel)

        now = _now()
        for u, (rtype, rid, rlabel) in recipients.items():
            if not _pref_allows(conn, u, f"new_post:{rtype}", "inapp"):
                continue
            conn.execute(
                "INSERT INTO notifications(username, type, actor, reason_type, "
                "reason_id, reason_label, slug, title, subtitle, read, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,0,?)",
                (u, "new_post", publisher, rtype, rid, rlabel, slug, title, subtitle, now),
            )
            created += 1
            # Phase 3 : si EMAIL_ENABLED et _pref_allows(...,'email'), file d'envoi.
        conn.execute(
            "INSERT INTO post_announcements(slug, announced_at) VALUES(?,?)",
            (slug, now),
        )
    return created


def ensure_seeded() -> None:
    """Marque « déjà annoncés » les posts publiés antérieurs à la Phase 2.

    Sans ça, la première publication déclencherait un fan-out rétroactif sur des
    albums anciens. Exécuté une fois (sentinelle `__seeded__`).
    """
    with get_conn() as conn:
        if conn.execute(
            "SELECT 1 FROM post_announcements WHERE slug='__seeded__'"
        ).fetchone():
            return
        now = _now()
        if PROJECTS_DIR.exists():
            for pdir in sorted(PROJECTS_DIR.iterdir()):
                mp = pdir / "manifest.yaml"
                if not mp.is_file():
                    continue
                try:
                    published = Manifest.load(mp).data.get("published", True)
                except Exception:
                    continue
                if published:
                    conn.execute(
                        "INSERT OR IGNORE INTO post_announcements(slug, announced_at) "
                        "VALUES(?,?)", (pdir.name, now),
                    )
        conn.execute(
            "INSERT OR IGNORE INTO post_announcements(slug, announced_at) "
            "VALUES('__seeded__', ?)", (now,),
        )


# =====================================================================
#  CENTRE DE NOTIFICATIONS (lecture / lu)
# =====================================================================
def _notif_dict(r) -> dict:
    return {
        "id": r["id"],
        "type": r["type"],
        "username": r["username"],
        "actor": r["actor"],
        "reason_type": r["reason_type"],
        "reason_id": r["reason_id"],
        "reason_label": r["reason_label"],
        "slug": r["slug"],
        "title": r["title"],
        "subtitle": r["subtitle"],
        "read": bool(r["read"]),
        "created_at": r["created_at"],
    }


def draft_reminders(username: str) -> list[dict]:
    """Rappels « brouillon en cours » de l'utilisateur, calculés à la volée.

    Volontairement **synthétiques** (aucune ligne en base) : un rappel n'est pas
    un évènement mais un état. Le stocker obligerait à le créer à l'import, le
    dédupliquer, puis le supprimer à la reprise/suppression du brouillon — avec
    le risque de rappels fantômes pour un projet qui n'existe plus. Recalculé à
    chaque lecture, le rappel apparaît et disparaît tout seul, et reste exact.

    Conséquence assumée : pas d'`id` numérique donc non marquable comme lu (le
    front les rend non cliquables-pour-lecture) — ils s'effacent en terminant ou
    supprimant le brouillon, ce qui est l'action attendue de toute façon.
    """
    from . import catalogue

    out: list[dict] = []
    try:
        drafts = catalogue.list_drafts()
    except Exception:
        return out
    for d in drafts:
        if d.get("imported_by") != username:
            continue
        out.append({
            "id": None,
            "type": "draft_pending",
            "username": username,
            "actor": username,
            "reason_type": "draft",
            "reason_id": d["slug"],
            "reason_label": "",
            "slug": d["slug"],
            "title": d.get("title") or d["slug"],
            "subtitle": d.get("artist", ""),
            "read": False,
            "created_at": d.get("updated_at") or d.get("imported_at") or "",
        })
    return out


@router.get("/api/social/notifications")
def list_notifications(offset: int = 0, limit: int = 20,
                       identity: dict = Depends(current_identity)) -> dict:
    username = identity.get("username")
    if not username:
        return {"authenticated": False, "unread": 0, "total": 0, "items": []}
    limit = max(1, min(limit, 50))
    with get_conn() as conn:
        unread = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE username=? AND read=0",
            (username,),
        ).fetchone()["n"]
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE username=?", (username,)
        ).fetchone()["n"]
        rows = conn.execute(
            "SELECT * FROM notifications WHERE username=? "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (username, limit, offset),
        ).fetchall()
    items = [_notif_dict(r) for r in rows]
    # Les rappels de brouillon coiffent la première page : ce sont des tâches en
    # attente, pas de l'historique — les noyer sous les notifications anciennes
    # (ou pire, les faire tomber page 2) raterait leur seul but.
    reminders = render_activity(username) + draft_reminders(username)
    if reminders:
        # `unread` volontairement inchangé : un rappel est un état permanent,
        # pas un évènement non lu. L'inclure rendrait la pastille inextinguible
        # — « tout marquer comme lu » ne peut rien contre un item sans id.
        total += len(reminders)
        if offset == 0:
            items = reminders + items
    return {"authenticated": True, "unread": unread, "total": total,
            "offset": offset, "items": items}


@router.get("/api/social/notifications/count")
def unread_count(identity: dict = Depends(current_identity)) -> dict:
    username = identity.get("username")
    if not username:
        return {"unread": 0}
    with get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE username=? AND read=0",
            (username,),
        ).fetchone()["n"]
    # Les rappels de brouillon sont exclus du compteur (cf. list_notifications).
    return {"unread": n}


class ReadIn(BaseModel):
    ids: list[int] | None = None
    all: bool = False


@router.post("/api/social/notifications/read")
def mark_read(payload: ReadIn, identity: dict = Depends(require_user)) -> dict:
    username = identity["username"]
    with get_conn() as conn:
        if payload.all:
            conn.execute(
                "UPDATE notifications SET read=1 WHERE username=? AND read=0",
                (username,),
            )
        elif payload.ids:
            qs = ",".join("?" * len(payload.ids))
            conn.execute(
                f"UPDATE notifications SET read=1 WHERE username=? AND id IN ({qs})",
                (username, *payload.ids),
            )
        unread = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE username=? AND read=0",
            (username,),
        ).fetchone()["n"]
    # Même total que /count : les rappels de brouillon en sont exclus, sinon la
    # pastille resterait allumée après un tout-marquer-lu (ils n'ont pas d'id,
    # donc rien ne peut les marquer).
    return {"ok": True, "unread": unread}


# =====================================================================
#  PAGES HTML (coquille publique — état perso via /api/social/*)
# =====================================================================
def _page(name: str) -> HTMLResponse:
    page = FRONTEND / name
    if page.exists():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>live2mp3</h1>")


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page() -> HTMLResponse:
    return _page("notifications.html")


@router.get("/settings", response_class=HTMLResponse)
def settings_page() -> HTMLResponse:
    return _page("settings.html")
