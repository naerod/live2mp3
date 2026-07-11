"""Système social live2mp3 : profils, favoris (likes d'albums), commentaires.

Modèle de commentaires (style Reddit/Dealabs, aplati à 1 niveau) :
- commentaire racine  : `parent_id` NULL ;
- réponse             : `parent_id` = id du commentaire racine du fil ;
  répondre à une réponse rattache au même fil racine, avec `reply_to` = pseudo
  mentionné. L'UI affiche les racines triées par score/date, puis les réponses
  en fil chronologique avec un bouton « voir plus ».

Identité fournie par le forward-auth Authentik (voir `auth.py`). Les lectures
sont publiques (identité optionnelle pour l'état perso : vote, like) ; les
écritures exigent une session authentifiée (`require_user`).
"""
from __future__ import annotations

import io
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from . import catalogue
from .auth import (
    GROUP_GESTIONNAIRE,
    SUPERUSER_GROUPS,
    current_identity,
    require_user,
)
from .db import AVATARS_DIR, get_conn
from .manifest import PROJECTS_DIR

router = APIRouter()

BASE = Path(__file__).resolve().parent.parent
FRONTEND = BASE / "frontend"

BODY_MAX = 4000
REPLY_PAGE = 3          # réponses affichées d'emblée sous une racine
TOP_PAGE = 20           # commentaires racine par page
AVATAR_MAX_BYTES = 8 * 1024 * 1024


# --- Helpers ---------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_moderator(groups: set[str]) -> bool:
    return bool(groups & SUPERUSER_GROUPS) or GROUP_GESTIONNAIRE in groups


def _safe_username(username: str) -> str:
    """Jeton de nom de fichier sûr pour l'avatar (anti-traversée)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", username or "")[:120] or "user"


def _avatar_path(username: str) -> Path:
    return AVATARS_DIR / f"{_safe_username(username)}.webp"


def _album_exists(slug: str) -> bool:
    return (PROJECTS_DIR / slug / "manifest.yaml").exists()


def _catalogue_map(include_drafts: bool = False) -> dict[str, dict]:
    return {a["slug"]: a for a in catalogue.list_albums(include_drafts=include_drafts)}


def _album_label(slug: str, cat: dict[str, dict]) -> dict:
    """Titre + artiste d'un album pour l'affichage d'un commentaire.

    Priorité au catalogue (albums publiés) ; repli sur le manifest brut pour
    couvrir un album non encore rendu ou dépublié ; défaut = slug.
    """
    a = cat.get(slug)
    if a:
        return {"title": a.get("title", slug), "artist": a.get("artist", "")}
    mpath = PROJECTS_DIR / slug / "manifest.yaml"
    if mpath.exists():
        try:
            from .manifest import Manifest
            alb = Manifest.load(mpath).data.get("album", {})
            return {"title": alb.get("title", slug), "artist": alb.get("artist", "")}
        except Exception:
            pass
    return {"title": slug, "artist": ""}


def _profiles_map(conn: sqlite3.Connection, usernames: set[str]) -> dict[str, dict]:
    """display_name + présence d'avatar par username (sans créer de profil)."""
    out: dict[str, dict] = {}
    for u in usernames:
        out[u] = {"display_name": u, "avatar": False}
    if not usernames:
        return out
    qs = ",".join("?" * len(usernames))
    rows = conn.execute(
        f"SELECT username, display_name, avatar_ext FROM profiles "
        f"WHERE username IN ({qs})",
        tuple(usernames),
    ).fetchall()
    for r in rows:
        out[r["username"]] = {
            "display_name": r["display_name"] or r["username"],
            "avatar": bool(r["avatar_ext"]),
        }
    return out


def _ensure_profile(conn: sqlite3.Connection, username: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM profiles WHERE username=?", (username,)
    ).fetchone()
    if row:
        return row
    now = _now()
    conn.execute(
        "INSERT INTO profiles(username, display_name, bio, avatar_ext, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?)",
        (username, username, "", "", now, now),
    )
    return conn.execute(
        "SELECT * FROM profiles WHERE username=?", (username,)
    ).fetchone()


def _scores(conn: sqlite3.Connection, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    qs = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT comment_id, COALESCE(SUM(value),0) AS s FROM comment_votes "
        f"WHERE comment_id IN ({qs}) GROUP BY comment_id",
        tuple(ids),
    ).fetchall()
    return {r["comment_id"]: r["s"] for r in rows}


def _my_votes(conn: sqlite3.Connection, ids: list[int], username: str | None) -> dict[int, int]:
    if not ids or not username:
        return {}
    qs = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT comment_id, value FROM comment_votes "
        f"WHERE username=? AND comment_id IN ({qs})",
        (username, *ids),
    ).fetchall()
    return {r["comment_id"]: r["value"] for r in rows}


def _comment_dict(row: sqlite3.Row, profiles: dict[str, dict],
                  scores: dict[int, int], my_votes: dict[int, int]) -> dict:
    deleted = bool(row["deleted"])
    prof = profiles.get(row["username"], {"display_name": row["username"], "avatar": False})
    return {
        "id": row["id"],
        "username": None if deleted else row["username"],
        "display_name": None if deleted else prof["display_name"],
        "avatar": False if deleted else prof["avatar"],
        "reply_to": row["reply_to"] or "",
        "body": "" if deleted else row["body"],
        "deleted": deleted,
        "created_at": row["created_at"],
        "edited_at": row["edited_at"] or "",
        "score": scores.get(row["id"], 0),
        "my_vote": my_votes.get(row["id"], 0),
    }


# --- Modèles ---------------------------------------------------------------
class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=BODY_MAX)
    parent_id: int | None = None


class CommentEditIn(BaseModel):
    body: str = Field(min_length=1, max_length=BODY_MAX)


class VoteIn(BaseModel):
    value: int  # -1, 0, +1


class ProfileIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=40)
    bio: str = Field(default="", max_length=500)


# =====================================================================
#  PROFIL
# =====================================================================
@router.get("/api/social/me")
def social_me(identity: dict = Depends(current_identity)) -> dict:
    username = identity.get("username")
    if not username:
        return {"authenticated": False}
    with get_conn() as conn:
        row = _ensure_profile(conn, username)
    return {
        "authenticated": True,
        "username": username,
        "display_name": row["display_name"],
        "bio": row["bio"],
        "avatar": bool(row["avatar_ext"]),
        "is_moderator": _is_moderator(identity.get("groups", set())),
    }


@router.get("/api/social/profiles")
def batch_profiles(u: str = "", identity: dict = Depends(current_identity)) -> dict:
    """Résout en un appel {username: {display_name, avatar}} — pour afficher
    l'avatar du posteur sur les cartes/fiches d'album."""
    users = {x.strip() for x in u.split(",") if x.strip()}
    if len(users) > 100:
        users = set(list(users)[:100])
    with get_conn() as conn:
        return _profiles_map(conn, users)


@router.get("/api/social/users/{username}")
def user_profile(username: str, identity: dict = Depends(current_identity)) -> dict:
    viewer = identity.get("username")
    is_self = viewer == username
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM profiles WHERE username=?", (username,)
        ).fetchone()
        # Un utilisateur "existe" s'il a un profil, un commentaire, un favori,
        # ou au moins un album publié.
        has_activity = row is not None
        if not has_activity:
            has_activity = conn.execute(
                "SELECT 1 FROM comments WHERE username=? LIMIT 1", (username,)
            ).fetchone() is not None
        if not has_activity:
            has_activity = conn.execute(
                "SELECT 1 FROM favorites WHERE username=? LIMIT 1", (username,)
            ).fetchone() is not None

        cat_all = _catalogue_map(include_drafts=is_self)
        publications = [a for a in cat_all.values() if a.get("imported_by") == username]
        publications.sort(key=lambda a: a.get("date", ""), reverse=True)
        if not has_activity and not publications:
            raise HTTPException(404, "utilisateur introuvable")

        # Favoris (likes d'albums) — uniquement les albums encore publiés/visibles.
        cat_pub = _catalogue_map(include_drafts=False)
        fav_rows = conn.execute(
            "SELECT slug FROM favorites WHERE username=? ORDER BY created_at DESC",
            (username,),
        ).fetchall()
        likes = [cat_pub[r["slug"]] for r in fav_rows if r["slug"] in cat_pub]

        # Commentaires de l'utilisateur (les plus récents).
        crows = conn.execute(
            "SELECT * FROM comments WHERE username=? AND deleted=0 "
            "ORDER BY created_at DESC LIMIT 50",
            (username,),
        ).fetchall()
        ids = [r["id"] for r in crows]
        scores = _scores(conn, ids)
        my_votes = _my_votes(conn, ids, viewer)
        comments = []
        for r in crows:
            alb = _album_label(r["slug"], cat_pub)
            comments.append({
                "id": r["id"],
                "slug": r["slug"],
                "album_title": alb["title"],
                "album_artist": alb["artist"],
                "body": r["body"],
                "created_at": r["created_at"],
                "edited_at": r["edited_at"] or "",
                "score": scores.get(r["id"], 0),
                "my_vote": my_votes.get(r["id"], 0),
                "is_reply": r["parent_id"] is not None,
            })

    prof = {
        "username": username,
        "display_name": (row["display_name"] if row else username),
        "bio": (row["bio"] if row else ""),
        "avatar": bool(row["avatar_ext"]) if row else False,
        "created_at": (row["created_at"] if row else ""),
    }
    return {
        "profile": prof,
        "is_self": is_self,
        "counts": {
            "publications": len(publications),
            "comments": len(comments),
            "likes": len(likes),
        },
        "publications": publications,
        "comments": comments,
        "likes": likes,
    }


@router.put("/api/social/profile")
def update_profile(payload: ProfileIn, identity: dict = Depends(require_user)) -> dict:
    username = identity["username"]
    with get_conn() as conn:
        _ensure_profile(conn, username)
        conn.execute(
            "UPDATE profiles SET display_name=?, bio=?, updated_at=? WHERE username=?",
            (payload.display_name.strip(), payload.bio.strip(), _now(), username),
        )
    return {"ok": True, "display_name": payload.display_name.strip(), "bio": payload.bio.strip()}


@router.post("/api/social/profile/avatar")
async def upload_avatar(file: UploadFile = File(...),
                        identity: dict = Depends(require_user)) -> dict:
    username = identity["username"]
    raw = await file.read()
    if len(raw) > AVATAR_MAX_BYTES:
        raise HTTPException(413, "image trop lourde (max 8 Mo)")
    try:
        from PIL import Image, ImageOps
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im)
        im = ImageOps.fit(im, (512, 512), method=Image.LANCZOS, centering=(0.5, 0.5))
        im = im.convert("RGB")
    except Exception:
        raise HTTPException(400, "image invalide")
    AVATARS_DIR.mkdir(parents=True, exist_ok=True)
    im.save(_avatar_path(username), "WEBP", quality=82, method=6)
    with get_conn() as conn:
        _ensure_profile(conn, username)
        conn.execute(
            "UPDATE profiles SET avatar_ext='webp', updated_at=? WHERE username=?",
            (_now(), username),
        )
    return {"ok": True, "avatar": True}


@router.delete("/api/social/profile/avatar")
def delete_avatar(identity: dict = Depends(require_user)) -> dict:
    username = identity["username"]
    _avatar_path(username).unlink(missing_ok=True)
    with get_conn() as conn:
        conn.execute(
            "UPDATE profiles SET avatar_ext='', updated_at=? WHERE username=?",
            (_now(), username),
        )
    return {"ok": True, "avatar": False}


@router.get("/avatar/{username}")
def get_avatar(username: str) -> FileResponse:
    path = _avatar_path(username)
    if not path.exists():
        raise HTTPException(404, "pas d'avatar")
    return FileResponse(path, media_type="image/webp")


# =====================================================================
#  FAVORIS (likes d'albums)
# =====================================================================
def _album_social(conn: sqlite3.Connection, slug: str, username: str | None) -> dict:
    likes = conn.execute(
        "SELECT COUNT(*) AS n FROM favorites WHERE slug=?", (slug,)
    ).fetchone()["n"]
    liked = False
    if username:
        liked = conn.execute(
            "SELECT 1 FROM favorites WHERE slug=? AND username=?", (slug, username)
        ).fetchone() is not None
    comments = conn.execute(
        "SELECT COUNT(*) AS n FROM comments WHERE slug=? AND deleted=0", (slug,)
    ).fetchone()["n"]
    return {"likes": likes, "liked": liked, "comments": comments}


@router.get("/api/social/albums/{slug}")
def album_social(slug: str, identity: dict = Depends(current_identity)) -> dict:
    with get_conn() as conn:
        return _album_social(conn, slug, identity.get("username"))


@router.post("/api/social/albums/{slug}/like")
def toggle_like(slug: str, identity: dict = Depends(require_user)) -> dict:
    username = identity["username"]
    if not _album_exists(slug):
        raise HTTPException(404, "album introuvable")
    with get_conn() as conn:
        _ensure_profile(conn, username)
        existing = conn.execute(
            "SELECT 1 FROM favorites WHERE slug=? AND username=?", (slug, username)
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM favorites WHERE slug=? AND username=?", (slug, username)
            )
        else:
            conn.execute(
                "INSERT INTO favorites(username, slug, created_at) VALUES(?,?,?)",
                (username, slug, _now()),
            )
        return _album_social(conn, slug, username)


# =====================================================================
#  COMMENTAIRES
# =====================================================================
@router.get("/api/social/albums/{slug}/comments")
def list_comments(slug: str, sort: str = "top", offset: int = 0, limit: int = TOP_PAGE,
                  identity: dict = Depends(current_identity)) -> dict:
    viewer = identity.get("username")
    limit = max(1, min(limit, 50))
    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM comments WHERE slug=? AND parent_id IS NULL",
            (slug,),
        ).fetchone()["n"]
        tops = conn.execute(
            "SELECT * FROM comments WHERE slug=? AND parent_id IS NULL",
            (slug,),
        ).fetchall()
        top_ids = [r["id"] for r in tops]

        # Réponses de tout le fil (pour compter + première page).
        replies_by_top: dict[int, list[sqlite3.Row]] = {i: [] for i in top_ids}
        if top_ids:
            qs = ",".join("?" * len(top_ids))
            rrows = conn.execute(
                f"SELECT * FROM comments WHERE parent_id IN ({qs}) "
                f"ORDER BY created_at ASC",
                tuple(top_ids),
            ).fetchall()
            for r in rrows:
                replies_by_top[r["parent_id"]].append(r)

        all_ids = top_ids + [r["id"] for rows in replies_by_top.values() for r in rows]
        scores = _scores(conn, all_ids)
        my_votes = _my_votes(conn, all_ids, viewer)
        usernames = {r["username"] for r in tops}
        for rows in replies_by_top.values():
            usernames |= {r["username"] for r in rows}
        profiles = _profiles_map(conn, usernames)

        # Tri des racines.
        if sort == "new":
            tops.sort(key=lambda r: r["created_at"], reverse=True)
        else:  # "top"
            tops.sort(key=lambda r: (scores.get(r["id"], 0), r["created_at"]), reverse=True)
        tops_page = tops[offset:offset + limit]

        out = []
        for t in tops_page:
            replies = replies_by_top.get(t["id"], [])
            item = _comment_dict(t, profiles, scores, my_votes)
            item["reply_count"] = len(replies)
            item["replies"] = [
                _comment_dict(r, profiles, scores, my_votes) for r in replies[:REPLY_PAGE]
            ]
            out.append(item)
    return {"sort": sort, "total": total, "offset": offset, "comments": out}


@router.get("/api/social/comments/{comment_id}/replies")
def list_replies(comment_id: int, offset: int = 0, limit: int = 20,
                 identity: dict = Depends(current_identity)) -> dict:
    viewer = identity.get("username")
    limit = max(1, min(limit, 50))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM comments WHERE parent_id=? ORDER BY created_at ASC "
            "LIMIT ? OFFSET ?",
            (comment_id, limit, offset),
        ).fetchall()
        ids = [r["id"] for r in rows]
        scores = _scores(conn, ids)
        my_votes = _my_votes(conn, ids, viewer)
        profiles = _profiles_map(conn, {r["username"] for r in rows})
        replies = [_comment_dict(r, profiles, scores, my_votes) for r in rows]
    return {"replies": replies, "offset": offset}


@router.post("/api/social/albums/{slug}/comments")
def create_comment(slug: str, payload: CommentIn,
                   identity: dict = Depends(require_user)) -> dict:
    username = identity["username"]
    if not _album_exists(slug):
        raise HTTPException(404, "album introuvable")
    body = payload.body.strip()
    if not body:
        raise HTTPException(400, "commentaire vide")
    parent_id = None
    reply_to = ""
    with get_conn() as conn:
        _ensure_profile(conn, username)
        if payload.parent_id is not None:
            parent = conn.execute(
                "SELECT * FROM comments WHERE id=?", (payload.parent_id,)
            ).fetchone()
            if not parent or parent["slug"] != slug or parent["deleted"]:
                raise HTTPException(404, "commentaire parent introuvable")
            # Aplatissement à 1 niveau : rattache toujours à la racine du fil.
            parent_id = parent["id"] if parent["parent_id"] is None else parent["parent_id"]
            reply_to = parent["username"]
        cur = conn.execute(
            "INSERT INTO comments(slug, parent_id, reply_to, username, body, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (slug, parent_id, reply_to, username, body, _now()),
        )
        new_id = cur.lastrowid
        row = conn.execute("SELECT * FROM comments WHERE id=?", (new_id,)).fetchone()
        profiles = _profiles_map(conn, {username})
    return _comment_dict(row, profiles, {new_id: 0}, {new_id: 0})


@router.patch("/api/social/comments/{comment_id}")
def edit_comment(comment_id: int, payload: CommentEditIn,
                 identity: dict = Depends(require_user)) -> dict:
    username = identity["username"]
    body = payload.body.strip()
    if not body:
        raise HTTPException(400, "commentaire vide")
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
        if not row or row["deleted"]:
            raise HTTPException(404, "commentaire introuvable")
        if row["username"] != username:
            raise HTTPException(403, "modification interdite")
        conn.execute(
            "UPDATE comments SET body=?, edited_at=? WHERE id=?",
            (body, _now(), comment_id),
        )
        row = conn.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
        scores = _scores(conn, [comment_id])
        my_votes = _my_votes(conn, [comment_id], username)
        profiles = _profiles_map(conn, {username})
    return _comment_dict(row, profiles, scores, my_votes)


@router.delete("/api/social/comments/{comment_id}")
def delete_comment(comment_id: int, identity: dict = Depends(require_user)) -> dict:
    username = identity["username"]
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
        if not row or row["deleted"]:
            raise HTTPException(404, "commentaire introuvable")
        if row["username"] != username and not _is_moderator(identity.get("groups", set())):
            raise HTTPException(403, "suppression interdite")
        # Suppression douce : préserve le fil, masque contenu et auteur.
        conn.execute(
            "UPDATE comments SET deleted=1, body='', edited_at=? WHERE id=?",
            (_now(), comment_id),
        )
    return {"ok": True, "id": comment_id, "deleted": True}


@router.post("/api/social/comments/{comment_id}/vote")
def vote_comment(comment_id: int, payload: VoteIn,
                 identity: dict = Depends(require_user)) -> dict:
    username = identity["username"]
    value = payload.value
    if value not in (-1, 0, 1):
        raise HTTPException(400, "vote invalide")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, deleted FROM comments WHERE id=?", (comment_id,)
        ).fetchone()
        if not row or row["deleted"]:
            raise HTTPException(404, "commentaire introuvable")
        _ensure_profile(conn, username)
        if value == 0:
            conn.execute(
                "DELETE FROM comment_votes WHERE comment_id=? AND username=?",
                (comment_id, username),
            )
        else:
            conn.execute(
                "INSERT INTO comment_votes(comment_id, username, value) VALUES(?,?,?) "
                "ON CONFLICT(comment_id, username) DO UPDATE SET value=excluded.value",
                (comment_id, username, value),
            )
        score = _scores(conn, [comment_id]).get(comment_id, 0)
    return {"ok": True, "id": comment_id, "score": score, "my_vote": value}


# =====================================================================
#  PAGE PROFIL (HTML public)
# =====================================================================
@router.get("/u/{username}", response_class=HTMLResponse)
def profile_page(username: str) -> HTMLResponse:
    page = FRONTEND / "profile.html"
    if page.exists():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>live2mp3 — profil</h1>")
