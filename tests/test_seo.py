"""SEO de la vitrine : métadonnées, robots.txt, sitemap.xml."""
import pytest
from fastapi.testclient import TestClient

GEST = {"X-authentik-username": "mod", "X-authentik-groups": "live2mp3-gestionnaire"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend import catalogue, db, main, manifest, social
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "d")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "d" / "x.db")
    for mod in (manifest, main, catalogue, social):
        monkeypatch.setattr(mod, "PROJECTS_DIR", tmp_path)
    db.init_db()
    return TestClient(main.app)


def test_vitrine_metadonnees(client):
    html = client.get("/").text
    for needle in ('<html lang="fr"', "<title>", 'name="description"',
                   'property="og:title"', 'property="og:description"',
                   'name="viewport"', 'rel="canonical"'):
        assert needle in html, needle


def test_robots_prod_et_preprod(client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    r = client.get("/robots.txt").text
    assert "Disallow: /api/" in r and "Sitemap:" in r and "Disallow: /\n" not in r
    monkeypatch.setenv("APP_ENV", "preprod")
    assert client.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"


def test_sitemap_suit_le_catalogue_public(client, monkeypatch):
    from backend import catalogue
    seen = {}

    def fake(sort="date_concert", include_drafts=False):
        seen["drafts"] = include_drafts
        return [{"slug": "muse-2026-01-01"}, {"slug": "a&b"}]
    monkeypatch.setattr(catalogue, "list_albums", fake)
    r = client.get("/sitemap.xml")
    assert r.headers["content-type"].startswith("application/xml")
    assert seen["drafts"] is False                       # jamais de brouillon
    assert "/album/muse-2026-01-01</loc>" in r.text
    assert "/album/a%26b</loc>" in r.text                 # encodé, XML valide
