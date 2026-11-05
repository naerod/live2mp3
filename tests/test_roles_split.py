"""« Connecté » (tout compte Authentik) ≠ « téléchargement » (live2mp3-user)."""
import pytest
from fastapi.testclient import TestClient

GEST = {"X-authentik-username": "mod", "X-authentik-groups": "live2mp3-gestionnaire"}
NEWBIE = {"X-authentik-username": "newbie", "X-authentik-groups": "authentik Default"}
USER = {"X-authentik-username": "alice", "X-authentik-groups": "live2mp3-user"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend import catalogue, covers, db, main, manifest, social
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "live2mp3.db")
    monkeypatch.setattr(db, "AVATARS_DIR", data / "avatars")
    monkeypatch.setattr(social, "AVATARS_DIR", data / "avatars")
    for mod in (manifest, main, catalogue, social, covers):
        monkeypatch.setattr(mod, "PROJECTS_DIR", tmp_path)
    db.init_db()
    c = TestClient(main.app)
    payload = {"album": {"artist": "A", "title": "Live", "date": "2026-01-01"},
               "tracks": [{"n": 1, "title": "Song"}], "target": "data_disc"}
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    c.patch(f"/api/albums/{slug}/published", json={"published": True}, headers=GEST)
    return c, slug


def test_nouvel_inscrit_peut_liker_commenter_suivre(client):
    c, slug = client
    assert c.post(f"/api/social/albums/{slug}/like", headers=NEWBIE).status_code == 200
    r = c.post(f"/api/social/albums/{slug}/comments", json={"body": "bravo"}, headers=NEWBIE)
    assert r.status_code == 200
    assert c.post(f"/api/social/comments/{r.json()['id']}/like", headers=NEWBIE).status_code == 200


def test_nouvel_inscrit_telecharge(client):
    """Règle 2026-11-05 : tout compte connecté télécharge ; l'anonyme non."""
    c, slug = client
    for path in (f"/download/{slug}/mp3", f"/download/{slug}/track/1"):
        assert c.get(path, headers=NEWBIE).status_code not in (401, 403)
        assert c.get(path).status_code == 401


def test_anonyme_toujours_refuse(client):
    c, slug = client
    assert c.post(f"/api/social/albums/{slug}/like").status_code == 401


def test_me_distingue_connexion_et_telechargement(client):
    c, _ = client
    me = c.get("/api/me", headers=NEWBIE).json()
    assert me["authenticated"] is True and me["can_download"] is True
    me = c.get("/api/me", headers=USER).json()
    assert me["authenticated"] is True and me["can_download"] is True
    me = c.get("/api/me").json()
    assert me["authenticated"] is False and me["can_download"] is False
    sm = c.get("/api/social/me", headers=NEWBIE).json()
    assert sm["authenticated"] is True and sm["can_download"] is True


def test_pseudo_non_modifiable_localement(client):
    c, _ = client
    before = c.get("/api/social/me", headers=USER).json()["display_name"]
    r = c.put("/api/social/profile", json={"display_name": "Usurpateur", "bio": "salut"},
              headers=USER)
    assert r.status_code == 200
    assert r.json()["display_name"] == before
    me = c.get("/api/social/me", headers=USER).json()
    assert me["display_name"] == before and me["bio"] == "salut"
    # Client à jour : n'envoie plus le pseudo.
    assert c.put("/api/social/profile", json={"bio": "b2"}, headers=USER).status_code == 200
