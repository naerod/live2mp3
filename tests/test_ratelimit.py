"""Anti-abus : débit par client (429) et refus d'écriture disque plein (507)."""
import pytest
from fastapi.testclient import TestClient

GEST = {"X-authentik-username": "mod", "X-authentik-groups": "live2mp3-gestionnaire"}
USER = {"X-authentik-username": "alice", "X-authentik-groups": "live2mp3-user"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend import catalogue, db, main, manifest, ratelimit, social
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "live2mp3.db")
    for mod in (manifest, main, catalogue, social, ratelimit):
        monkeypatch.setattr(mod, "PROJECTS_DIR", tmp_path)
    db.init_db()
    # Pile reconstruite = nouveau limiteur (fenêtre vide) pour chaque test.
    main.app.middleware_stack = None
    return TestClient(main.app)


def test_lecture_publique_limitee_par_ip(client, monkeypatch):
    monkeypatch.setenv("L2M_RATE_READ", "5")
    codes = [client.get("/api/catalogue").status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    r = client.get("/api/catalogue")
    assert int(r.headers["retry-after"]) >= 1
    # Santé jamais limitée (sonde du hub).
    assert client.get("/healthz").status_code == 200


def test_ecriture_limitee_par_utilisateur(client, monkeypatch):
    monkeypatch.setenv("L2M_RATE_WRITE", "3")
    codes = [client.put("/api/social/profile", json={"bio": "x"}, headers=USER).status_code
             for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    # Un autre utilisateur a son propre quota.
    bob = {"X-authentik-username": "bob", "X-authentik-groups": "live2mp3-user"}
    assert client.put("/api/social/profile", json={"bio": "y"}, headers=bob).status_code == 200


def test_xff_ignore_hors_proxy_de_confiance():
    from backend.ratelimit import client_key
    scope = {"client": ("203.0.113.9", 1), "headers": [(b"x-forwarded-for", b"1.2.3.4")]}
    assert client_key(scope) == "ip:203.0.113.9"      # XFF forgeable : ignoré
    import os
    os.environ["L2M_TRUSTED_PROXIES"] = "203.0.113.0/24"
    try:
        assert client_key(scope) == "ip:1.2.3.4"
    finally:
        del os.environ["L2M_TRUSTED_PROXIES"]


def test_disque_plein_refuse_les_depots_mais_pas_la_lecture(client, monkeypatch):
    monkeypatch.setenv("L2M_MIN_FREE_MB", str(10 ** 12))   # « jamais assez »
    payload = {"album": {"artist": "A", "title": "T", "date": "2026-01-01"},
               "tracks": [{"n": 1, "title": "S"}], "target": "data_disc"}
    assert client.post("/api/jobs", json=payload, headers=GEST).status_code == 507
    assert client.post("/api/social/profile/avatar",
                       files={"file": ("a.png", b"x", "image/png")},
                       headers=USER).status_code == 507
    assert client.get("/api/catalogue").status_code == 200
    # Écriture sans fichier (profil texte) toujours possible.
    assert client.put("/api/social/profile", json={"bio": "x"}, headers=USER).status_code == 200
