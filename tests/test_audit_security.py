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


# --- 3. Pas d'écriture sociale sur un album invisible ----------------------
def test_ecritures_sociales_refusees_sur_brouillon(client):
    c, root = client
    slug = _make_album(c)
    cid = c.post(f"/api/social/albums/{slug}/comments", json={"body": "hello"},
                 headers=USER).json()["id"]
    c.post(f"/api/social/albums/{slug}/comments",
           json={"body": "re", "parent_id": cid}, headers=USER2)
    # Dépublication : l'album n'existe plus pour un simple utilisateur.
    c.patch(f"/api/albums/{slug}/published", json={"published": False}, headers=GEST)
    assert c.post(f"/api/social/albums/{slug}/like", headers=USER).status_code == 404
    assert c.post(f"/api/social/albums/{slug}/comments", json={"body": "x"},
                  headers=USER).status_code == 404
    assert c.post(f"/api/social/comments/{cid}/like", headers=USER).status_code == 404
    assert c.get(f"/api/social/comments/{cid}/replies").status_code == 404
    import io
    png = io.BytesIO()
    from PIL import Image
    Image.new("RGB", (10, 10)).save(png, "PNG")
    r = c.post(f"/api/social/albums/{slug}/covers",
               files={"cover": ("c.png", png.getvalue(), "image/png")}, headers=USER)
    assert r.status_code == 404
    # Le gestionnaire, lui, garde la main.
    assert c.post(f"/api/social/albums/{slug}/like", headers=GEST).status_code == 200
    assert c.get(f"/api/social/comments/{cid}/replies", headers=GEST).status_code == 200


def test_offset_negatif_ignore(client):
    c, _ = client
    slug = _make_album(c)
    for i in range(3):
        c.post(f"/api/social/albums/{slug}/comments", json={"body": f"c{i}"},
               headers=USER)
    base = f"/api/social/albums/{slug}/comments?sort=new&limit=1"
    r = c.get(base + "&offset=-1")
    assert r.status_code == 200
    # offset=-1 ≡ offset=0 (avant : la tranche Python renvoyait le dernier).
    assert r.json()["comments"] == c.get(base + "&offset=0").json()["comments"]
    assert r.json()["offset"] == 0


# --- 4. Pochette : pas de sortie du dossier projet -------------------------
@pytest.mark.parametrize("cover", ["../../etc/passwd", "/etc/passwd",
                                   "artwork/../../x", "a\\..\\b"])
def test_job_refuse_cover_hors_projet(client, cover):
    c, _ = client
    payload = {"album": {"artist": "Z", "title": "T", "date": "2026-02-02",
                         "cover": cover},
               "tracks": [{"n": 1, "title": "S"}], "target": "data_disc"}
    assert c.post("/api/jobs", json=payload, headers=GEST).status_code == 422


def test_manifest_altere_ne_sert_pas_hors_projet(client):
    """Défense en profondeur : un manifest déjà altéré ne fuit rien."""
    from backend.manifest import Manifest
    c, root = client
    slug = _make_album(c)
    secret = root / "secret.txt"
    secret.write_text("TOP SECRET")
    m = Manifest.load(root / slug / "manifest.yaml")
    m.data["album"]["cover"] = "../secret.txt"
    m.save(touch=False)
    for url in (f"/download/{slug}/cover", f"/download/{slug}/cover/pdf",
                f"/cover/{slug}"):
        r = c.get(url, headers=USER)
        assert r.status_code == 404, url
        assert b"TOP SECRET" not in r.content


def test_cover_relative_valide_acceptee(client):
    c, root = client
    payload = {"album": {"artist": "Z", "title": "T", "date": "2026-02-03",
                         "cover": "artwork/cover.jpg"},
               "tracks": [{"n": 1, "title": "S"}], "target": "data_disc"}
    assert c.post("/api/jobs", json=payload, headers=GEST).status_code == 200

