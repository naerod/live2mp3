"""Entités canoniques : dérivation depuis un album + page auto d'entité."""
import pytest
from fastapi.testclient import TestClient

from backend import entities

GEST = {"X-authentik-username": "mod", "X-authentik-groups": "live2mp3-gestionnaire"}
USER = {"X-authentik-username": "alice", "X-authentik-groups": "live2mp3-user"}


# --- Dérivation pure -------------------------------------------------------
def test_album_entities_all_types():
    album = {
        "artist": "Coldplay", "artist_id": "892",
        "guests": [{"id": "27", "name": "Beyoncé"}],
        "festival": "Glastonbury 2024", "venue": "Pyramid Stage",
    }
    ents = entities.album_entities(album)
    keys = {f"{e['type']}:{e['id']}" for e in ents}
    assert "artist:892" in keys              # principal
    assert "artist:27" in keys               # invité
    assert "festival:glastonbury-2024" in keys   # slug dérivé du libellé
    assert "venue:pyramid-stage" in keys


def test_album_entities_ignores_incomplete():
    # Un artiste sans id n'est pas un lien suivable ; un festival sans libellé non plus.
    assert entities.album_entities({"artist": "X"}) == []
    assert entities.album_entities({"artist_id": "1"}) == []       # pas de libellé
    # festival_id explicite respecté
    ents = entities.album_entities({"festival": "Fest", "festival_id": "custom"})
    assert ents == [{"type": "festival", "id": "custom", "label": "Fest"}]


# --- Intégration : catalogue + page d'entité -------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch, synth_audio_only):
    from backend import catalogue, covers, db, main, manifest, social, suggest
    from backend.pipeline import render
    # Métadonnées canoniques + rendu MP3 (pour apparaître au catalogue).
    m = manifest.Manifest.load(synth_audio_only / "manifest.yaml")
    m.data["album"].update({
        "artist_id": "892",
        "festival": "Glastonbury 2024",
        "venue": "Pyramid Stage",
    })
    m.save()
    render.run(synth_audio_only, video=False)

    root = synth_audio_only.parent
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "live2mp3.db")
    monkeypatch.setattr(db, "AVATARS_DIR", data / "avatars")
    monkeypatch.setattr(social, "AVATARS_DIR", data / "avatars")
    for mod in (manifest, main, catalogue, social):
        monkeypatch.setattr(mod, "PROJECTS_DIR", root)
    monkeypatch.setattr(covers, "PROJECTS_DIR", root)
    monkeypatch.setattr(suggest, "resolve_artist",
                        lambda i: {"id": i, "label": "Test Artist", "picture": "http://pic"}
                        if i == "892" else None)
    db.init_db()
    return TestClient(main.app), synth_audio_only.name


def test_catalogue_exposes_entities(client):
    c, slug = client
    cat = c.get("/api/catalogue").json()
    card = next(a for a in cat if a["slug"] == slug)
    keys = {f"{e['type']}:{e['id']}" for e in card["entities"]}
    assert "artist:892" in keys
    assert "festival:glastonbury-2024" in keys
    assert "venue:pyramid-stage" in keys


def test_entity_page_lists_posts(client):
    c, slug = client
    d = c.get("/api/social/entity/artist/892").json()
    assert d["label"] == "Test Artist" and d["picture"] == "http://pic"
    assert d["post_count"] == 1 and d["posts"][0]["slug"] == slug
    assert d["followers"] == 0 and d["following"] is False
    # Suivre depuis la page, puis re-lire l'état.
    c.post("/api/social/follow", json={"target_type": "artist", "target_id": "892"}, headers=USER)
    d = c.get("/api/social/entity/artist/892", headers=USER).json()
    assert d["following"] is True and d["followers"] == 1


def test_festival_page(client):
    c, slug = client
    d = c.get("/api/social/entity/festival/glastonbury-2024").json()
    assert d["label"] == "Glastonbury 2024" and d["post_count"] == 1


def test_unknown_entity_404(client):
    c, _ = client
    assert c.get("/api/social/entity/artist/000000").status_code == 404
    assert c.get("/api/social/entity/badtype/x").status_code == 404


def test_festival_suggest(client):
    c, _ = client
    items = c.get("/api/social/suggest/festivals?q=glas", headers=GEST).json()
    assert any(it["id"] == "glastonbury-2024" for it in items)
