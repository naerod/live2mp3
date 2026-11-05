"""Régressions de sécurité / contrôle d'accès relevées par l'audit (2026-11).

Chaque test épingle un correctif précis : rôle oublié, fuite inter-environnement,
traversée de chemin, injection d'URL, limites d'entrée.
"""
import pytest
from fastapi.testclient import TestClient

GEST = {"X-authentik-username": "mod", "X-authentik-groups": "live2mp3-gestionnaire"}
APP_ADMIN = {"X-authentik-username": "boss", "X-authentik-groups": "live2mp3-admin"}
USER = {"X-authentik-username": "alice", "X-authentik-groups": "live2mp3-user"}
USER2 = {"X-authentik-username": "bob", "X-authentik-groups": "live2mp3-user"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend import catalogue, covers, db, main, manifest, social
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "live2mp3.db")
    monkeypatch.setattr(db, "AVATARS_DIR", data / "avatars")
    monkeypatch.setattr(social, "AVATARS_DIR", data / "avatars")
    monkeypatch.setattr(manifest, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(main, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(catalogue, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(social, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(covers, "PROJECTS_DIR", tmp_path)
    db.init_db()
    return TestClient(main.app), tmp_path


def _make_album(c, artist="A", title="Live", date="2026-01-01", publish=True) -> str:
    payload = {"album": {"artist": artist, "title": title, "date": date},
               "tracks": [{"n": 1, "title": "Song", "start": 0.0, "end": 3.0,
                           "locked": True}],
               "target": "data_disc"}
    r = c.post("/api/jobs", json=payload, headers=GEST)
    assert r.status_code == 200, r.text
    slug = r.json()["slug"]
    if publish:
        c.patch(f"/api/albums/{slug}/published", json={"published": True},
                headers=GEST)
    return slug


# --- 1. L'admin applicatif est un gestionnaire partout ---------------------
def test_helper_gestionnaire_inclut_admin_applicatif():
    from backend.auth import is_gestionnaire_identity
    assert is_gestionnaire_identity({"groups": {"live2mp3-admin"}})
    assert is_gestionnaire_identity({"groups": {"live2mp3-gestionnaire"}})
    assert is_gestionnaire_identity({"groups": {"authentik Admins"}})
    assert is_gestionnaire_identity({"is_gestionnaire": True})
    assert not is_gestionnaire_identity({"groups": {"live2mp3-user"}})
    assert not is_gestionnaire_identity({})
    assert not is_gestionnaire_identity(None)


def test_admin_applicatif_voit_les_brouillons(client):
    c, _ = client
    slug = _make_album(c, publish=False)
    assert c.get(f"/api/catalogue/{slug}", headers=USER).status_code == 404
    assert c.get(f"/api/catalogue/{slug}", headers=APP_ADMIN).status_code == 200
    assert c.get(f"/api/social/albums/{slug}", headers=APP_ADMIN).status_code == 200


def test_admin_applicatif_modere_les_commentaires(client):
    c, _ = client
    slug = _make_album(c)
    cid = c.post(f"/api/social/albums/{slug}/comments", json={"body": "spam"},
                 headers=USER).json()["id"]
    assert c.delete(f"/api/social/comments/{cid}", headers=USER2).status_code == 403
    r = c.delete(f"/api/social/comments/{cid}", headers=APP_ADMIN)
    assert r.status_code == 200 and r.json()["deleted"] is True


# --- 2. Album né en preprod : invisible en prod, même par URL directe ------
def _tag_preprod(root, slug):
    from backend.manifest import Manifest
    m = Manifest.load(root / slug / "manifest.yaml")
    m.data["origin_env"] = "preprod"
    m.save(touch=False)


def test_album_preprod_introuvable_en_prod_par_url(client, monkeypatch):
    from backend import catalogue
    c, root = client
    slug = _make_album(c)
    _tag_preprod(root, slug)
    monkeypatch.setattr(catalogue, "APP_ENV", "prod")
    for url in (f"/api/catalogue/{slug}", f"/api/social/albums/{slug}",
                f"/api/social/albums/{slug}/comments"):
        assert c.get(url).status_code == 404, url
        assert c.get(url, headers=GEST).status_code == 404, url
    assert c.get(f"/download/{slug}/mp3", headers=USER).status_code == 404
    # La preprod, elle, le voit.
    monkeypatch.setattr(catalogue, "APP_ENV", "preprod")
    assert c.get(f"/api/catalogue/{slug}").status_code == 200
