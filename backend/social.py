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

from . import catalogue, suggest
from .auth import (
    GROUP_GESTIONNAIRE,
    GROUP_USER,
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
MAX_LIKERS = 5          # avatars affichés dans la rangée de likes


# --- Helpers ---------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_moderator(groups: set[str]) -> bool:
    return bool(groups & SUPERUSER_GROUPS) or GROUP_GESTIONNAIRE in groups


def _role_of(groups: set[str]) -> str:
    """Rôle canonique (le plus élevé) à partir des groupes Authentik."""
    if groups & SUPERUSER_GROUPS:
        return "admin"
    if GROUP_GESTIONNAIRE in groups:
        return "gestionnaire"
    if GROUP_USER in groups:
        return "user"
    return ""


def _touch_role(conn: sqlite3.Connection, username: str, groups: set[str]) -> str:
    """Met à jour le rôle en cache si les groupes ont changé. Retourne le rôle."""
    role = _role_of(groups)
    if not role:
        return ""
    row = conn.execute("SELECT role FROM profiles WHERE username=?", (username,)).fetchone()
    if row is not None and row["role"] != role:
        conn.execute(
            "UPDATE profiles SET role=?, updated_at=? WHERE username=?",
            (role, _now(), username),
        )
    return role


def _safe_username(username: str) -> str:
    """Jeton de nom de fichier sûr pour l'avatar (anti-traversée)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", username or "")[:120] or "user"


def _avatar_path(username: str) -> Path:
    return AVATARS_DIR / f"{_safe_username(username)}.webp"


def _album_exists(slug: str) -> bool:
    return (PROJECTS_DIR / slug / "manifest.yaml").exists()


def _album_visible(slug: str, identity: dict | None) -> bool:
    """Un album dépublié n'est visible que des gestionnaires.

    Utilisé par les lectures sociales (fiche, commentaires, pochettes) pour ne
    pas révéler l'existence d'un brouillon au public — même réponse 404 qu'un
    slug inconnu.
    """
    path = PROJECTS_DIR / slug / "manifest.yaml"
    if not path.exists():
        return False
    from .manifest import Manifest
    try:
        published = Manifest.load(path).data.get("published", True)
    except Exception:
        published = True
    if published:
        return True
    from .auth import GROUP_GESTIONNAIRE, SUPERUSER_GROUPS
    identity = identity or {}
    if identity.get("is_gestionnaire"):
        return True
    groups = set(identity.get("groups") or [])
    return bool(groups & ({GROUP_GESTIONNAIRE} | SUPERUSER_GROUPS))


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


def _comment_likes(
    conn: sqlite3.Connection, ids: list[int], username: str | None = None
) -> dict[int, dict]:
    """Retourne dict[comment_id → {likes, liked, likers}] pour un lot d'ids."""
    if not ids:
        return {}
    qs = ",".join("?" * len(ids))
    counts = {
        r["comment_id"]: r["n"]
        for r in conn.execute(
            f"SELECT comment_id, COUNT(*) AS n FROM comment_likes "
            f"WHERE comment_id IN ({qs}) GROUP BY comment_id",
            tuple(ids),
        ).fetchall()
    }
    likers_by_id: dict[int, list[str]] = {i: [] for i in ids}
    for cid in ids:
        rows = conn.execute(
            "SELECT username FROM comment_likes WHERE comment_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (cid, MAX_LIKERS),
        ).fetchall()
        likers_by_id[cid] = [r["username"] for r in rows]
    all_likers = {u for ul in likers_by_id.values() for u in ul}
    profs = _profiles_map(conn, all_likers)
    liked_set: set[int] = set()
    if username:
        liked_set = {
            r["comment_id"]
            for r in conn.execute(
                f"SELECT comment_id FROM comment_likes "
                f"WHERE username=? AND comment_id IN ({qs})",
                (username, *ids),
            ).fetchall()
        }
    out: dict[int, dict] = {}
    for cid in ids:
        out[cid] = {
            "likes": counts.get(cid, 0),
            "liked": cid in liked_set,
            "likers": [
                {
                    "username": u,
                    "display_name": profs.get(u, {}).get("display_name", u),
                    "avatar": profs.get(u, {}).get("avatar", False),
                }
                for u in likers_by_id[cid]
            ],
        }
    return out


def _comment_dict(row: sqlite3.Row, profiles: dict[str, dict],
                  likes_map: dict[int, dict]) -> dict:
    deleted = bool(row["deleted"])
    prof = profiles.get(row["username"], {"display_name": row["username"], "avatar": False})
    ld = likes_map.get(row["id"], {"likes": 0, "liked": False, "likers": []})
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
        "likes": ld["likes"],
        "liked": ld["liked"],
        "likers": ld["likers"],
    }


# --- Modèles ---------------------------------------------------------------
class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=BODY_MAX)
    parent_id: int | None = None
    cover_id: int | None = None   # None = commentaire d'album


class CommentEditIn(BaseModel):
    body: str = Field(min_length=1, max_length=BODY_MAX)


class ProfileIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=40)
    bio: str = Field(default="", max_length=500)
    # Identifiants canoniques uniquement (code INSEE / id Deezer) : le libellé
    # affiché est reconstruit côté serveur, jamais dicté par le client — c'est
    # ce qui garantit un formalisme unique. Chaîne vide = champ effacé.
    city_id: str = Field(default="", max_length=16)
    artist_id: str = Field(default="", max_length=32)


# =====================================================================
#  PROFIL
# =====================================================================
@router.get("/api/social/me")
def social_me(identity: dict = Depends(current_identity)) -> dict:
    username = identity.get("username")
    if not username:
        return {"authenticated": False}
    groups = identity.get("groups", set())
    with get_conn() as conn:
        row = _ensure_profile(conn, username)
        # Capture le rôle à chaque passage authentifié (le header init l'appelle
        # sur chaque page) → badge de rôle disponible sur n'importe quel profil.
        role = _touch_role(conn, username, groups)
    is_super = bool(groups & SUPERUSER_GROUPS)
    is_gest = is_super or GROUP_GESTIONNAIRE in groups
    is_user = is_gest or GROUP_USER in groups
    return {
        "authenticated": bool(username) and is_user,
        "username": username,
        "display_name": row["display_name"],
        "bio": row["bio"],
        "avatar": bool(row["avatar_ext"]),
        "city_id": row["city_id"], "city": row["city_label"],
        "artist_id": row["artist_id"], "artist": row["artist_label"],
        "is_moderator": _is_moderator(groups),
        # Droits (mêmes champs que /api/me) : permet au header de n'appeler que
        # cet endpoint, supprimant un aller-retour par navigation.
        "is_user": is_user, "is_gestionnaire": is_gest, "is_admin": is_super,
        "role": role,
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
        likes_map = _comment_likes(conn, ids, viewer)
        comments = []
        for r in crows:
            alb = _album_label(r["slug"], cat_pub)
            ld = likes_map.get(r["id"], {"likes": 0, "liked": False, "likers": []})
            comments.append({
                "id": r["id"],
                "slug": r["slug"],
                "album_title": alb["title"],
                "album_artist": alb["artist"],
                "body": r["body"],
                "created_at": r["created_at"],
                "edited_at": r["edited_at"] or "",
                "likes": ld["likes"],
                "liked": ld["liked"],
                "is_reply": r["parent_id"] is not None,
            })

        # État de suivi de ce profil par le visiteur (bouton Suivre/cloche).
        followers = conn.execute(
            "SELECT COUNT(*) AS n FROM follows WHERE target_type='user' AND target_id=?",
            (username,),
        ).fetchone()["n"]
        following = notify = False
        if viewer and not is_self:
            frow = conn.execute(
                "SELECT notify FROM follows WHERE username=? AND target_type='user' "
                "AND target_id=?",
                (viewer, username),
            ).fetchone()
            if frow is not None:
                following = True
                notify = bool(frow["notify"])

    prof = {
        "username": username,
        "display_name": (row["display_name"] if row else username),
        "bio": (row["bio"] if row else ""),
        "avatar": bool(row["avatar_ext"]) if row else False,
        "city_id": (row["city_id"] if row else ""),
        "city": (row["city_label"] if row else ""),
        "artist_id": (row["artist_id"] if row else ""),
        "artist": (row["artist_label"] if row else ""),
        "role": (row["role"] if row else ""),
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
        "follow": {"followers": followers, "following": following, "notify": notify},
        "publications": publications,
        "comments": comments,
        "likes": likes,
    }


def _resolve_choice(kind: str, new_id: str, row: sqlite3.Row) -> tuple[str, str]:
    """(id, label) à écrire pour un champ à suggestions.

    Ne sollicite la source que si l'id a *changé* : éditer sa bio ne doit pas
    dépendre de la disponibilité d'une API tierce, ni en payer la latence.
    """
    new_id = (new_id or "").strip()
    old_id = (row[f"{kind}_id"] or "") if row is not None else ""
    if not new_id:
        return "", ""
    if new_id == old_id and row[f"{kind}_label"]:
        return old_id, row[f"{kind}_label"]
    resolver = suggest.resolve_city if kind == "city" else suggest.resolve_artist
    try:
        entry = resolver(new_id)
    except suggest.SourceUnavailable:
        raise HTTPException(503, f"source de suggestions {kind} indisponible")
    if not entry:
        raise HTTPException(422, f"{kind} inconnu — choisis une entrée dans la liste")
    return entry["id"], entry["label"]


@router.put("/api/social/profile")
def update_profile(payload: ProfileIn, identity: dict = Depends(require_user)) -> dict:
    username = identity["username"]
    with get_conn() as conn:
        row = _ensure_profile(conn, username)
        city_id, city_label = _resolve_choice("city", payload.city_id, row)
        artist_id, artist_label = _resolve_choice("artist", payload.artist_id, row)
        conn.execute(
            "UPDATE profiles SET display_name=?, bio=?, city_id=?, city_label=?, "
            "artist_id=?, artist_label=?, updated_at=? WHERE username=?",
            (payload.display_name.strip(), payload.bio.strip(), city_id, city_label,
             artist_id, artist_label, _now(), username),
        )
    return {
        "ok": True,
        "display_name": payload.display_name.strip(),
        "bio": payload.bio.strip(),
        "city_id": city_id, "city": city_label,
        "artist_id": artist_id, "artist": artist_label,
    }


# =====================================================================
#  SUGGESTIONS (ville, artiste favori)
# =====================================================================
@router.get("/api/social/suggest/cities")
def suggest_cities(q: str = "", identity: dict = Depends(current_identity)) -> list[dict]:
    return suggest.search_cities(q)


@router.get("/api/social/suggest/artists")
def suggest_artists(q: str = "", identity: dict = Depends(current_identity)) -> list[dict]:
    return suggest.search_artists(q)


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

@router.get("/api/social/my-likes")
def my_likes(identity: dict = Depends(current_identity)) -> list:
    username = identity.get("username")
    if not username:
        return []
    with get_conn() as conn:
        rows = conn.execute("SELECT slug FROM favorites WHERE username=?", (username,)).fetchall()
    return [r["slug"] for r in rows]

@router.get("/api/social/counts")
def all_album_counts() -> dict:
    """Compteurs likes+commentaires pour tous les albums (un seul appel depuis la vitrine)."""
    with get_conn() as conn:
        likes_rows = conn.execute(
            "SELECT slug, COUNT(*) AS n FROM favorites GROUP BY slug"
        ).fetchall()
        comments_rows = conn.execute(
            "SELECT slug, COUNT(*) AS n FROM comments WHERE deleted=0 GROUP BY slug"
        ).fetchall()
    likes = {r["slug"]: r["n"] for r in likes_rows}
    comments = {r["slug"]: r["n"] for r in comments_rows}
    all_slugs = set(likes) | set(comments)
    return {s: {"likes": likes.get(s, 0), "comments": comments.get(s, 0)} for s in all_slugs}




@router.get("/api/social/albums/{slug}")
def album_social(slug: str, identity: dict = Depends(current_identity)) -> dict:
    if not _album_visible(slug, identity):
        raise HTTPException(404, "album introuvable")
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
                  cover_id: int | None = None,
                  identity: dict = Depends(current_identity)) -> dict:
    if not _album_visible(slug, identity):
        raise HTTPException(404, "album introuvable")
    """Fil de l'album (`cover_id` absent) ou fil d'une pochette (`cover_id` posé).

    Les deux partagent la même table : `cover_id IS ?` sélectionne l'un ou
    l'autre sans jamais les mélanger (`IS` compare NULL correctement).
    """
    viewer = identity.get("username")
    limit = max(1, min(limit, 50))
    # Un commentaire racine supprimé n'est affiché que s'il a au moins une
    # réponse non supprimée (préserver le contexte du fil).
    _TOP_FILTER = """
        slug=? AND cover_id IS ? AND parent_id IS NULL
        AND (deleted=0 OR EXISTS (
            SELECT 1 FROM comments r
            WHERE r.parent_id=comments.id AND r.deleted=0
        ))
    """
    scope = (slug, cover_id)
    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM comments WHERE {_TOP_FILTER}", scope
        ).fetchone()["n"]
        tops = conn.execute(
            f"SELECT * FROM comments WHERE {_TOP_FILTER}", scope
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
        likes_map = _comment_likes(conn, all_ids, viewer)
        usernames = {r["username"] for r in tops}
        for rows in replies_by_top.values():
            usernames |= {r["username"] for r in rows}
        profiles = _profiles_map(conn, usernames)

        # Tri des racines.
        if sort == "new":
            tops.sort(key=lambda r: r["created_at"], reverse=True)
        else:  # "top"
            tops.sort(
                key=lambda r: (likes_map.get(r["id"], {}).get("likes", 0), r["created_at"]),
                reverse=True,
            )
        tops_page = tops[offset:offset + limit]

        out = []
        for t in tops_page:
            replies = replies_by_top.get(t["id"], [])
            item = _comment_dict(t, profiles, likes_map)
            item["reply_count"] = len(replies)
            item["replies"] = [
                _comment_dict(r, profiles, likes_map) for r in replies[:REPLY_PAGE]
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
        likes_map = _comment_likes(conn, ids, viewer)
        profiles = _profiles_map(conn, {r["username"] for r in rows})
        replies = [_comment_dict(r, profiles, likes_map) for r in rows]
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
    cover_id = payload.cover_id
    with get_conn() as conn:
        _ensure_profile(conn, username)
        if cover_id is not None:
            cov = conn.execute(
                "SELECT slug FROM covers WHERE id=?", (cover_id,)
            ).fetchone()
            if not cov or cov["slug"] != slug:
                raise HTTPException(404, "pochette introuvable")
        if payload.parent_id is not None:
            parent = conn.execute(
                "SELECT * FROM comments WHERE id=?", (payload.parent_id,)
            ).fetchone()
            if not parent or parent["slug"] != slug or parent["deleted"]:
                raise HTTPException(404, "commentaire parent introuvable")
            # Une réponse reste dans le fil de son parent : sinon un commentaire
            # d'album pourrait se retrouver greffé sous une pochette.
            if parent["cover_id"] != cover_id:
                raise HTTPException(400, "réponse hors du fil du parent")
            # Aplatissement à 1 niveau : rattache toujours à la racine du fil.
            parent_id = parent["id"] if parent["parent_id"] is None else parent["parent_id"]
            reply_to = parent["username"]
        cur = conn.execute(
            "INSERT INTO comments(slug, cover_id, parent_id, reply_to, username, body, "
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (slug, cover_id, parent_id, reply_to, username, body, _now()),
        )
        new_id = cur.lastrowid
        row = conn.execute("SELECT * FROM comments WHERE id=?", (new_id,)).fetchone()
        profiles = _profiles_map(conn, {username})
    return _comment_dict(row, profiles, {new_id: {"likes": 0, "liked": False, "likers": []}})


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
        likes_map = _comment_likes(conn, [comment_id], username)
        profiles = _profiles_map(conn, {username})
    return _comment_dict(row, profiles, likes_map)


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


@router.post("/api/social/comments/{comment_id}/like")
def like_comment(comment_id: int, identity: dict = Depends(require_user)) -> dict:
    username = identity["username"]
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, deleted FROM comments WHERE id=?", (comment_id,)
        ).fetchone()
        if not row or row["deleted"]:
            raise HTTPException(404, "commentaire introuvable")
        _ensure_profile(conn, username)
        if conn.execute(
            "SELECT 1 FROM comment_likes WHERE comment_id=? AND username=?",
            (comment_id, username),
        ).fetchone():
            conn.execute(
                "DELETE FROM comment_likes WHERE comment_id=? AND username=?",
                (comment_id, username),
            )
        else:
            conn.execute(
                "INSERT INTO comment_likes(comment_id, username, created_at) VALUES(?,?,?)",
                (comment_id, username, _now()),
            )
        ld = _comment_likes(conn, [comment_id], username)[comment_id]
    return {"ok": True, "id": comment_id, **ld}


# =====================================================================
#  PAGE PROFIL (HTML public)
# =====================================================================
@router.get("/u/{username}", response_class=HTMLResponse)
def profile_page(username: str) -> HTMLResponse:
    page = FRONTEND / "profile.html"
    if page.exists():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>live2mp3 — profil</h1>")
