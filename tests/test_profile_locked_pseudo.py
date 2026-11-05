"""Pseudo verrouillé : seul le profil central (portail /profil) le modifie."""
from tests.test_audit_security import client  # noqa: F401

NEW = {"X-authentik-username": "newbie", "X-authentik-groups": ""}


def test_pseudo_ignore_par_le_site(client):  # noqa: F811
    c, _ = client
    before = c.get("/api/social/me", headers=NEW).json()["display_name"]
    r = c.put("/api/social/profile", headers=NEW,
              json={"display_name": "Usurpateur", "bio": "salut"})
    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == before
    me = c.get("/api/social/me", headers=NEW).json()
    assert me["display_name"] == before and me["bio"] == "salut"
    assert "display_name" in me["locked_fields"]
    assert me["profile_portal_url"].startswith("https://")


def test_profil_sans_pseudo_accepte(client):  # noqa: F811
    c, _ = client
    r = c.put("/api/social/profile", headers=NEW, json={"bio": "x"})
    assert r.status_code == 200, r.text
    prof = c.get("/api/social/users/newbie", headers=NEW)
    assert prof.status_code in (200, 404)
