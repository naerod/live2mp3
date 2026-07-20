"""Système de suivi + pages auto d'entités (artistes, festivals, lieux).

Modèle façon X/YouTube :
- **Suivre** crée une ligne `follows` (relation qui alimentera le feed) ;
- la **cloche** (`notify`) pilote les notifications ; elle est active par défaut
  au moment du suivi et se coupe sans dé-suivre.

Namespacé sous `/api/social/` : sur la prod, seul ce préfixe hérite du
soft-auth nginx (identité optionnelle en lecture, `require_user` en écriture) —
aucune modification nginx nécessaire à la promotion. Les pages HTML d'entités
(`/artist`, `/festival`, `/venue`) sont servies par `location /` (coquille
publique), l'état perso venant des appels `/api/social/*`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import catalogue, entities, suggest
from .auth import current_identity, require_user
from .db import get_conn

router = APIRouter()

BASE = Path(__file__).resolve().parent.parent
FRONTEND = BASE / "frontend"

LABEL_MAX = 120


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Modèles ---------------------------------------------------------------
class FollowIn(BaseModel):
    target_type: str
    target_id: str = Field(min_length=1, max_length=190)
    # Libellé d'affichage envoyé par le client (déjà connu à l'écran). Non
    # autoritatif : sert seulement la liste « Abonnements ». Pour un artiste, on
    # préfère de toute façon le libellé canonique Deezer.
    target_label: str = Field(default="", max_length=LABEL_MAX)


class NotifyIn(FollowIn):
    notify: bool


# --- Helpers ---------------------------------------------------------------
def _valid_type(t: str) -> str:
    if t not in entities.FOLLOW_TYPES:
        raise HTTPException(422, f"type de suivi invalide: {t!r}")
    return t


def _albums_for_entity(etype: str, eid: str, include_drafts: bool = False) -> list[dict]:
    """Posts (fiches album publiées) liés à une entité, plus récents d'abord."""
    key = f"{etype}:{eid}"
    return [
        a for a in catalogue.list_albums(include_drafts=include_drafts)
        if key in {f"{e['type']}:{e['id']}" for e in a.get("entities", [])}
    ]


def _resolve_label(etype: str, eid: str, albums: list[dict], fallback: str = "") -> str:
    """Libellé d'affichage d'une entité — jamais un id nu à l'écran."""
    for a in albums:
        for e in a.get("entities", []):
            if e["type"] == etype and e["id"] == eid and e["label"]:
                return e["label"]
    if fallback:
        return fallback
    with get_conn() as conn:
        row = conn.execute(
            "SELECT target_label FROM follows WHERE target_type=? AND target_id=? "
            "AND target_label!='' LIMIT 1",
            (etype, eid),
        ).fetchone()
    if row and row["target_label"]:
        return row["target_label"]
    return eid


def _artist_meta(eid: str) -> dict:
    """Libellé + photo d'un artiste via Deezer (best-effort, caché)."""
    try:
        entry = suggest.resolve_artist(eid)
    except suggest.SourceUnavailable:
        return {}
    return entry or {}


def _follow_state(conn, username: str | None, etype: str, eid: str) -> dict:
    followers = conn.execute(
        "SELECT COUNT(*) AS n FROM follows WHERE target_type=? AND target_id=?",
        (etype, eid),
    ).fetchone()["n"]
    following = False
    notify = False
    if username:
        row = conn.execute(
            "SELECT notify FROM follows WHERE username=? AND target_type=? AND target_id=?",
            (username, etype, eid),
        ).fetchone()
        if row is not None:
            following = True
            notify = bool(row["notify"])
    return {"followers": followers, "following": following, "notify": notify}


# =====================================================================
#  SUIVI (écriture)
# =====================================================================
@router.post("/api/social/follow")
def toggle_follow(payload: FollowIn, identity: dict = Depends(require_user)) -> dict:
    username = identity["username"]
    etype = _valid_type(payload.target_type)
    eid = payload.target_id.strip()
    if etype == "user" and eid == username:
        raise HTTPException(400, "on ne se suit pas soi-même")

    # Libellé stocké : canonique pour un artiste, sinon celui fourni par le client.
    label = payload.target_label.strip()
    if etype == "artist":
        meta = _artist_meta(eid)
        if meta.get("label"):
            label = meta["label"]

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM follows WHERE username=? AND target_type=? AND target_id=?",
            (username, etype, eid),
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM follows WHERE username=? AND target_type=? AND target_id=?",
                (username, etype, eid),
            )
        else:
            conn.execute(
                "INSERT INTO follows(username, target_type, target_id, target_label, "
                "notify, created_at) VALUES(?,?,?,?,1,?)",
                (username, etype, eid, label[:LABEL_MAX], _now()),
            )
        state = _follow_state(conn, username, etype, eid)
    return {"ok": True, "target_type": etype, "target_id": eid, **state}


@router.patch("/api/social/follow/notify")
def set_notify(payload: NotifyIn, identity: dict = Depends(require_user)) -> dict:
    """La cloche : active/coupe les notifications. Activer implique suivre."""
    username = identity["username"]
    etype = _valid_type(payload.target_type)
    eid = payload.target_id.strip()
    label = payload.target_label.strip()
    if etype == "artist":
        meta = _artist_meta(eid)
        if meta.get("label"):
            label = meta["label"]
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM follows WHERE username=? AND target_type=? AND target_id=?",
            (username, etype, eid),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE follows SET notify=? WHERE username=? AND target_type=? AND target_id=?",
                (1 if payload.notify else 0, username, etype, eid),
            )
        else:
            # Activer la cloche sur une entité non suivie = la suivre d'emblée.
            conn.execute(
                "INSERT INTO follows(username, target_type, target_id, target_label, "
                "notify, created_at) VALUES(?,?,?,?,?,?)",
                (username, etype, eid, label[:LABEL_MAX],
                 1 if payload.notify else 0, _now()),
            )
        state = _follow_state(conn, username, etype, eid)
    return {"ok": True, "target_type": etype, "target_id": eid, **state}


# =====================================================================
#  SUIVI (lecture)
# =====================================================================
@router.get("/api/social/follows")
def my_follows(identity: dict = Depends(current_identity)) -> dict:
    """Abonnements de l'utilisateur, groupés par type (pour le profil / futur feed)."""
    username = identity.get("username")
    if not username:
        return {"authenticated": False, "artist": [], "festival": [], "venue": [], "user": []}
    out: dict[str, list] = {"artist": [], "festival": [], "venue": [], "user": []}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT target_type, target_id, target_label, notify, created_at "
            "FROM follows WHERE username=? ORDER BY created_at DESC",
            (username,),
        ).fetchall()
    for r in rows:
        out.setdefault(r["target_type"], []).append({
            "id": r["target_id"],
            "label": r["target_label"] or r["target_id"],
            "notify": bool(r["notify"]),
        })
    return {"authenticated": True, **out}


@router.get("/api/social/follow-state")
def follow_state(target_type: str, target_id: str,
                 identity: dict = Depends(current_identity)) -> dict:
    etype = _valid_type(target_type)
    with get_conn() as conn:
        state = _follow_state(conn, identity.get("username"), etype, target_id.strip())
    return {"target_type": etype, "target_id": target_id.strip(), **state}


@router.get("/api/social/entity/{etype}/{eid}")
def entity_data(etype: str, eid: str,
                identity: dict = Depends(current_identity)) -> dict:
    """Données d'une page auto d'entité : en-tête + posts liés + état de suivi."""
    if etype not in entities.ENTITY_TYPES:
        raise HTTPException(404, "type d'entité inconnu")
    eid = eid.strip()
    is_gest = bool(identity.get("is_gestionnaire"))
    albums = _albums_for_entity(etype, eid, include_drafts=False)
    label = _resolve_label(etype, eid, albums)
    picture = ""
    if etype == "artist":
        meta = _artist_meta(eid)
        if meta:
            label = meta.get("label") or label
            # Grand format pour l'en-tête (évite le flou de la miniature 56px).
            picture = meta.get("picture_hd") or meta.get("picture") or ""
    with get_conn() as conn:
        state = _follow_state(conn, identity.get("username"), etype, eid)
    # Une entité sans aucun post publié et que personne ne suit n'existe pas.
    if not albums and not state["followers"] and label == eid:
        raise HTTPException(404, "entité introuvable")
    return {
        "type": etype,
        "id": eid,
        "label": label,
        "picture": picture,
        "posts": albums,
        "post_count": len(albums),
        **state,
        "is_gestionnaire": is_gest,
    }


# =====================================================================
#  SUGGESTIONS (festival, lieu) — dérivées du catalogue existant
# =====================================================================
def _distinct_entities(etype: str) -> list[dict]:
    seen: dict[str, str] = {}
    for a in catalogue.list_albums():
        for e in a.get("entities", []):
            if e["type"] == etype and e["id"] not in seen:
                seen[e["id"]] = e["label"]
    return [{"id": k, "label": v} for k, v in
            sorted(seen.items(), key=lambda kv: kv[1].lower())]


@router.get("/api/social/suggest/festivals")
def suggest_festivals(q: str = "", identity: dict = Depends(current_identity)) -> list[dict]:
    """Festivals déjà saisis, filtrés par `q`. Pas de source externe : la liste
    s'auto-construit au fil des albums ; un festival inédit se crée en texte
    libre (l'id est alors dérivé du libellé côté serveur)."""
    ql = (q or "").strip().lower()
    items = _distinct_entities("festival")
    if ql:
        items = [it for it in items if ql in it["label"].lower()]
    return items[:8]


@router.get("/api/social/suggest/venues")
def suggest_venues(q: str = "", identity: dict = Depends(current_identity)) -> list[dict]:
    ql = (q or "").strip().lower()
    items = _distinct_entities("venue")
    if ql:
        items = [it for it in items if ql in it["label"].lower()]
    return items[:8]


# =====================================================================
#  PAGES HTML (coquille publique — état perso via /api/social/*)
# =====================================================================
def _entity_page() -> HTMLResponse:
    page = FRONTEND / "entity.html"
    if page.exists():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>live2mp3</h1>")


@router.get("/artist/{eid}", response_class=HTMLResponse)
def artist_page(eid: str) -> HTMLResponse:
    return _entity_page()


@router.get("/festival/{eid}", response_class=HTMLResponse)
def festival_page(eid: str) -> HTMLResponse:
    return _entity_page()


@router.get("/venue/{eid}", response_class=HTMLResponse)
def venue_page(eid: str) -> HTMLResponse:
    return _entity_page()
