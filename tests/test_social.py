"""Système social : profils, favoris (likes), commentaires (votes, réponses)."""
import io

import pytest
from fastapi.testclient import TestClient

GEST = {"X-authentik-username": "mod", "X-authentik-groups": "live2mp3-gestionnaire"}
USER = {"X-authentik-username": "alice", "X-authentik-groups": "live2mp3-user"}
USER2 = {"X-authentik-username": "bob", "X-authentik-groups": "live2mp3-user"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend import catalogue, db, main, manifest, social
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "live2mp3.db")
    monkeypatch.setattr(db, "AVATARS_DIR", data / "avatars")
    monkeypatch.setattr(social, "AVATARS_DIR", data / "avatars")
    monkeypatch.setattr(manifest, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(main, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(catalogue, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(social, "PROJECTS_DIR", tmp_path)
    db.init_db()
    return TestClient(main.app), tmp_path


def _make_album(c, artist="A", title="Live", date="2026-01-01", headers=GEST) -> str:
    payload = {"album": {"artist": artist, "title": title, "date": date},
               "tracks": [{"n": 1, "title": "Song", "start": 0.0, "end": 3.0, "locked": True}],
               "target": "data_disc"}
    return c.post("/api/jobs", json=payload, headers=headers).json()["slug"]


# --- Favoris / likes -------------------------------------------------------
def test_like_toggle_and_auth(client):
    c, _ = client
    slug = _make_album(c)
    assert c.post(f"/api/social/albums/{slug}/like").status_code == 401   # anonyme
    r = c.post(f"/api/social/albums/{slug}/like", headers=USER).json()
    assert r["liked"] is True and r["likes"] == 1
    r = c.post(f"/api/social/albums/{slug}/like", headers=USER).json()
    assert r["liked"] is False and r["likes"] == 0
    # like sur album inexistant -> 404
    assert c.post("/api/social/albums/nope/like", headers=USER).status_code == 404


def test_album_social_counts(client):
    c, _ = client
    slug = _make_album(c)
    c.post(f"/api/social/albums/{slug}/like", headers=USER)
    c.post(f"/api/social/albums/{slug}/like", headers=USER2)
    s = c.get(f"/api/social/albums/{slug}").json()
    assert s["likes"] == 2 and s["liked"] is False           # anonyme
    s = c.get(f"/api/social/albums/{slug}", headers=USER).json()
    assert s["liked"] is True


# --- Commentaires ----------------------------------------------------------
def test_comment_flatten_replies(client):
    c, _ = client
    slug = _make_album(c)
    assert c.post(f"/api/social/albums/{slug}/comments",
                  json={"body": "hi"}).status_code == 401           # anonyme
    top = c.post(f"/api/social/albums/{slug}/comments",
                 json={"body": "Super album"}, headers=USER).json()
    assert top["id"] and top["likes"] == 0 and top["reply_to"] == ""
    # réponse au commentaire racine
    r1 = c.post(f"/api/social/albums/{slug}/comments",
                json={"body": "d'accord", "parent_id": top["id"]}, headers=USER2).json()
    assert r1["reply_to"] == "alice"
    # réponse à la réponse -> aplatie sur la même racine, mention = bob
    r2 = c.post(f"/api/social/albums/{slug}/comments",
                json={"body": "merci", "parent_id": r1["id"]}, headers=USER).json()
    assert r2["reply_to"] == "bob"

    data = c.get(f"/api/social/albums/{slug}/comments").json()
    assert data["total"] == 1
    top_out = data["comments"][0]
    assert top_out["reply_count"] == 2
    assert len(top_out["replies"]) == 2
    # counts d'album : 3 commentaires
    assert c.get(f"/api/social/albums/{slug}").json()["comments"] == 3


def test_comment_like(client):
    c, _ = client
    slug = _make_album(c)
    cid = c.post(f"/api/social/albums/{slug}/comments",
                 json={"body": "x"}, headers=USER).json()["id"]
    # like par USER2
    r = c.post(f"/api/social/comments/{cid}/like", headers=USER2).json()
    assert r["likes"] == 1 and r["liked"] is True
    assert any(l["username"] == "bob" for l in r["likers"])
    # unlike (toggle)
    r = c.post(f"/api/social/comments/{cid}/like", headers=USER2).json()
    assert r["likes"] == 0 and r["liked"] is False
    # anonyme → 401
    assert c.post(f"/api/social/comments/{cid}/like").status_code == 401


def test_comment_edit_delete_permissions(client):
    c, _ = client
    slug = _make_album(c)
    cid = c.post(f"/api/social/albums/{slug}/comments",
                 json={"body": "original"}, headers=USER).json()["id"]
    # édition par un autre -> 403
    assert c.patch(f"/api/social/comments/{cid}",
                   json={"body": "hack"}, headers=USER2).status_code == 403
    r = c.patch(f"/api/social/comments/{cid}", json={"body": "corrigé"}, headers=USER).json()
    assert r["body"] == "corrigé" and r["edited_at"]
    # suppression par un autre user -> 403 ; par un modérateur -> ok
    assert c.delete(f"/api/social/comments/{cid}", headers=USER2).status_code == 403
    assert c.delete(f"/api/social/comments/{cid}", headers=GEST).json()["deleted"] is True
    # commentaire supprimé -> masqué des counts
    assert c.get(f"/api/social/albums/{slug}").json()["comments"] == 0


# --- Profils ---------------------------------------------------------------
def test_profile_me_and_update(client):
    c, _ = client
    assert c.get("/api/social/me").json()["authenticated"] is False
    me = c.get("/api/social/me", headers=USER).json()
    assert me["authenticated"] and me["display_name"] == "alice"
    r = c.put("/api/social/profile",
              json={"display_name": "Alice ♥", "bio": "fan de live"}, headers=USER).json()
    assert r["display_name"] == "Alice ♥"
    prof = c.get("/api/social/users/alice").json()
    assert prof["profile"]["display_name"] == "Alice ♥"
    assert prof["profile"]["bio"] == "fan de live"
    assert c.get("/api/social/users/alice", headers=USER).json()["is_self"] is True
    # utilisateur sans aucune activité -> 404
    assert c.get("/api/social/users/ghost").status_code == 404


def test_profile_shows_comments(client):
    c, _ = client
    slug = _make_album(c, artist="Muse", title="Live 26")
    c.post(f"/api/social/albums/{slug}/comments", json={"body": "top"}, headers=USER)
    prof = c.get("/api/social/users/alice").json()
    assert prof["counts"]["comments"] == 1
    assert prof["comments"][0]["album_artist"] == "Muse"


# --- Avatar ----------------------------------------------------------------
def test_avatar_upload_serve_delete(client):
    c, _ = client
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), (100, 120, 240)).save(buf, "PNG")
    buf.seek(0)
    assert c.post("/api/social/profile/avatar",
                  files={"file": ("a.png", b"notimage", "image/png")},
                  headers=USER).status_code == 400
    r = c.post("/api/social/profile/avatar",
               files={"file": ("a.png", buf.getvalue(), "image/png")}, headers=USER)
    assert r.status_code == 200 and r.json()["avatar"] is True
    img = c.get("/avatar/alice")
    assert img.status_code == 200 and img.headers["content-type"] == "image/webp"
    assert c.get("/api/social/me", headers=USER).json()["avatar"] is True
    assert c.delete("/api/social/profile/avatar", headers=USER).json()["avatar"] is False
    assert c.get("/avatar/alice").status_code == 404
