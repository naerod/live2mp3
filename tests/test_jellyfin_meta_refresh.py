"""Renommer un album/une piste doit atteindre Jellyfin (donc Finamp).

Incident 2026-09-19 : le titre d'un album changé dans live2mp3 restait figé
dans Finamp. Deux causes cumulées, verrouillées ici :
  1. `PUT /api/albums/{slug}/meta` ne rafraîchissait pas Jellyfin du tout ;
  2. `refresh_album()` envoyait `ReplaceAllMetadata=false`, avec quoi Jellyfin
     re-scanne les fichiers mais conserve le nom déjà en base.
"""
import pytest
import yaml
from fastapi.testclient import TestClient

GEST = {"X-authentik-username": "g", "X-authentik-groups": "live2mp3-gestionnaire"}


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
    return TestClient(main.app), tmp_path


@pytest.fixture
def refresh_calls(monkeypatch):
    from backend import main
    calls = []
    monkeypatch.setattr(
        main.jellyfin, "refresh_album",
        lambda slug, replace_metadata=False: calls.append((slug, replace_metadata)) or True,
    )
    return calls


def _seed(root, slug="a-2024-01-01"):
    d = root / slug
    d.mkdir(parents=True)
    (d / "manifest.yaml").write_text(yaml.safe_dump({
        "slug": slug,
        "target": "audio_cd",
        "album": {"artist": "A", "title": "Ancien titre", "date": "2024-01-01"},
        "source": {"url": "", "label": ""},
        "tracks": [{"n": 1, "title": "Un", "start": 0, "end": 3}],
    }), encoding="utf-8")
    return slug


def test_rename_album_refreshes_jellyfin_with_replace(client, refresh_calls):
    c, root = client
    slug = _seed(root)
    r = c.put(f"/api/albums/{slug}/meta", headers=GEST, json={
        "artist": "A", "title": "Nouveau titre", "date": "2024-01-01",
        "venue": "", "festival": "", "source_url": "", "source_label": "",
    })
    assert r.status_code == 200, r.text
    assert refresh_calls == [(slug, True)], refresh_calls


def test_rename_track_refreshes_jellyfin_with_replace(client, refresh_calls):
    c, root = client
    slug = _seed(root)
    r = c.patch(f"/api/albums/{slug}/tracks/1/meta", headers=GEST,
                json={"title": "Un (live)"})
    assert r.status_code == 200, r.text
    assert refresh_calls == [(slug, True)], refresh_calls


def test_refresh_album_sends_replace_all_metadata(monkeypatch):
    """Sans `ReplaceAllMetadata=true`, Jellyfin garde l'ancien nom en base."""
    from backend import jellyfin
    monkeypatch.setattr(jellyfin, "JELLYFIN_API_KEY", "k")
    monkeypatch.setattr(jellyfin, "_find_album_id", lambda slug: "ID")
    sent = {}

    class _R:
        status_code = 204

    def _post(url, params=None, headers=None, timeout=None):
        sent.update(params or {})
        return _R()

    monkeypatch.setattr(jellyfin.requests, "post", _post)
    assert jellyfin.refresh_album("s", replace_metadata=True) is True
    assert sent["ReplaceAllMetadata"] == "true"
    assert jellyfin.refresh_album("s") is True
    assert sent["ReplaceAllMetadata"] == "false"


def test_track_tag_is_bare_title_file_stays_numbered(client, refresh_calls, tmp_path):
    """Convention 2026-09-19 : tag nu, fichier numéroté.

    Le préfixe « 01. » faisait doublon dans Finamp, qui numérote déjà ses
    lignes ; il reste utile dans le nom de fichier (ZIP trié par nom).
    """
    from backend.albumfiles import _write_track_tags
    from backend.manifest import Manifest, numbered_title
    from mutagen.easyid3 import EasyID3

    c, root = client
    slug = _seed(root)
    audio = root / slug / "build" / "audio"
    audio.mkdir(parents=True)
    mp3 = audio / "01. Un.mp3"
    # En-tête MP3 minimal : mutagen doit pouvoir poser un tag ID3 dessus.
    mp3.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 2048)

    m = Manifest.load(root / slug / "manifest.yaml")
    assert _write_track_tags(slug, m) == 1
    assert EasyID3(str(mp3))["title"] == ["Un"]
    assert EasyID3(str(mp3))["tracknumber"] == ["1/1"]
    # La fonction de numérotation reste celle des noms de fichiers.
    assert numbered_title(1, "Un") == "01. Un"
