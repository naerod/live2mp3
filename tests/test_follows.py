"""Suivi (follow/cloche) + capture du rôle + état de suivi sur les profils."""
import pytest
from fastapi.testclient import TestClient

GEST = {"X-authentik-username": "mod", "X-authentik-groups": "live2mp3-gestionnaire"}
ADMIN = {"X-authentik-username": "root", "X-authentik-groups": "authentik Admins"}
USER = {"X-authentik-username": "alice", "X-authentik-groups": "live2mp3-user"}
USER2 = {"X-authentik-username": "bob", "X-authentik-groups": "live2mp3-user"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend import catalogue, covers, db, main, manifest, social, suggest
    data = tmp_path / "data"
    for mod in (db,):
        monkeypatch.setattr(mod, "DATA_DIR", data)
        monkeypatch.setattr(mod, "DB_PATH", data / "live2mp3.db")
        monkeypatch.setattr(mod, "AVATARS_DIR", data / "avatars")
    monkeypatch.setattr(social, "AVATARS_DIR", data / "avatars")
    for mod in (manifest, main, catalogue, social):
        monkeypatch.setattr(mod, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(covers, "PROJECTS_DIR", tmp_path)
    # Aucun appel réseau Deezer en test.
    monkeypatch.setattr(suggest, "resolve_artist", lambda i: {"id": i, "label": "Deezer " + i, "picture": ""})
    db.init_db()
    return TestClient(main.app)


# --- Follow / cloche -------------------------------------------------------
def test_follow_toggle_and_auth(client):
    c = client
    body = {"target_type": "artist", "target_id": "42", "target_label": "U2"}
    assert c.post("/api/social/follow", json=body).status_code == 401     # anonyme
    r = c.post("/api/social/follow", json=body, headers=USER).json()
    assert r["following"] is True and r["notify"] is True and r["followers"] == 1
    # dé-suivre (toggle)
    r = c.post("/api/social/follow", json=body, headers=USER).json()
    assert r["following"] is False and r["followers"] == 0


def test_no_self_follow(client):
    c = client
    r = c.post("/api/social/follow",
               json={"target_type": "user", "target_id": "alice"}, headers=USER)
    assert r.status_code == 400


def test_invalid_type(client):
    c = client
    r = c.post("/api/social/follow",
               json={"target_type": "banana", "target_id": "x"}, headers=USER)
    assert r.status_code == 422


def test_bell_toggle_and_autofollow(client):
    c = client
    tgt = {"target_type": "festival", "target_id": "glastonbury", "target_label": "Glastonbury"}
    # Activer la cloche sans suivre au préalable = suivre d'emblée.
    r = c.patch("/api/social/follow/notify", json={**tgt, "notify": True}, headers=USER).json()
    assert r["following"] is True and r["notify"] is True
    # Couper la cloche : suivi conservé, notifications off.
    r = c.patch("/api/social/follow/notify", json={**tgt, "notify": False}, headers=USER).json()
    assert r["following"] is True and r["notify"] is False


def test_my_follows_grouping(client):
    c = client
    c.post("/api/social/follow", json={"target_type": "artist", "target_id": "42", "target_label": "U2"}, headers=USER)
    c.post("/api/social/follow", json={"target_type": "festival", "target_id": "hellfest", "target_label": "Hellfest"}, headers=USER)
    c.post("/api/social/follow", json={"target_type": "user", "target_id": "bob"}, headers=USER)
    d = c.get("/api/social/follows", headers=USER).json()
    assert d["authenticated"] is True
    assert [x["id"] for x in d["artist"]] == ["42"]
    assert [x["id"] for x in d["festival"]] == ["hellfest"]
    assert [x["id"] for x in d["user"]] == ["bob"]
    # L'artiste stocke le libellé canonique Deezer (résolu au suivi).
    assert d["artist"][0]["label"] == "Deezer 42"


def test_follows_anon(client):
    d = client.get("/api/social/follows").json()
    assert d["authenticated"] is False and d["artist"] == []


def test_follow_state(client):
    c = client
    c.post("/api/social/follow", json={"target_type": "artist", "target_id": "42"}, headers=USER)
    c.post("/api/social/follow", json={"target_type": "artist", "target_id": "42"}, headers=USER2)
    s = c.get("/api/social/follow-state?target_type=artist&target_id=42").json()
    assert s["followers"] == 2 and s["following"] is False              # anonyme
    s = c.get("/api/social/follow-state?target_type=artist&target_id=42", headers=USER).json()
    assert s["following"] is True


# --- Rôle en cache ---------------------------------------------------------
def test_role_capture_and_badge(client):
    c = client
    # Le rôle est capturé au passage authentifié (social/me appelé par le header).
    assert c.get("/api/social/me", headers=GEST).json()["role"] == "gestionnaire"
    assert c.get("/api/social/me", headers=ADMIN).json()["role"] == "admin"
    assert c.get("/api/social/me", headers=USER).json()["role"] == "user"
    # Le rôle est ensuite visible sur le profil public.
    prof = c.get("/api/social/users/mod").json()
    assert prof["profile"]["role"] == "gestionnaire"


def test_user_follow_state_on_profile(client):
    c = client
    c.get("/api/social/me", headers=USER2)             # crée le profil bob
    # alice suit bob
    c.post("/api/social/follow", json={"target_type": "user", "target_id": "bob"}, headers=USER)
    prof = c.get("/api/social/users/bob", headers=USER).json()
    assert prof["follow"]["following"] is True and prof["follow"]["followers"] == 1
    # bob voit son propre profil : pas de bouton suivre (following False, followers 1)
    prof = c.get("/api/social/users/bob", headers=USER2).json()
    assert prof["is_self"] is True and prof["follow"]["followers"] == 1


def test_effective_role_publisher_floor():
    from backend.social import _effective_role
    pubs = {"nathan", "louis"}
    # Cache autoritaire prioritaire (ex: admin déjà capturé).
    assert _effective_role("admin", "naerod", pubs) == "admin"
    # Rôle vide + a publié -> plancher gestionnaire.
    assert _effective_role("", "nathan", pubs) == "gestionnaire"
    # Rôle vide + n'a jamais publié -> inconnu (pas de badge).
    assert _effective_role("", "alice", pubs) == ""


def test_artist_pics_batch(client, monkeypatch):
    from backend import follows
    monkeypatch.setattr(follows.suggest, "resolve_artist",
                        lambda i: {"id": i, "label": "A" + i, "picture": f"http://pic/{i}.jpg"})
    r = client.get("/api/social/artist-pics?ids=892,163,892").json()
    assert r == {"892": "http://pic/892.jpg", "163": "http://pic/163.jpg"}
    # id sans photo -> absent
    monkeypatch.setattr(follows.suggest, "resolve_artist", lambda i: {"id": i, "label": "x", "picture": ""})
    assert client.get("/api/social/artist-pics?ids=999").json() == {}


def test_profile_following_and_followers_lists(client):
    c = client
    c.get("/api/social/me", headers=USER2)             # crée bob
    # alice suit un artiste + bob
    c.post("/api/social/follow",
           json={"target_type": "artist", "target_id": "42", "target_label": "U2"}, headers=USER)
    c.post("/api/social/follow",
           json={"target_type": "user", "target_id": "bob", "target_label": "Bob"}, headers=USER)

    # Profil d'alice : ses abonnements groupés par type.
    prof = c.get("/api/social/users/alice", headers=USER).json()
    assert prof["counts"]["following"] == 2 and prof["counts"]["followers"] == 0
    fl = prof["following_list"]
    assert [i["id"] for i in fl["artist"]] == ["42"]
    assert fl["user"][0]["id"] == "bob"
    # Vu par alice (=viewer), chaque ligne porte son propre état de suivi.
    assert fl["artist"][0]["viewer_following"] is True
    assert fl["artist"][0]["viewer_notify"] is True

    # Profil de bob : alice apparaît dans ses abonnés, avec l'état du visiteur.
    prof = c.get("/api/social/users/bob", headers=USER).json()
    assert prof["counts"]["followers"] == 1
    fw = prof["followers_list"]
    assert fw[0]["id"] == "alice" and fw[0]["type"] == "user"
    # Le visiteur (alice) ne suit pas alice → viewer_following False.
    assert fw[0]["viewer_following"] is False
