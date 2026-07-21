"""Correction d'URL (renommage de slug) : migration des données sociales,
renommage du dossier projet et redirection 301 de l'ancienne URL."""
import pytest
from fastapi.testclient import TestClient

GEST = {"X-authentik-username": "mod", "X-authentik-groups": "live2mp3-gestionnaire"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend import catalogue, covers, db, main, manifest, social, slugrename
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "live2mp3.db")
    monkeypatch.setattr(db, "AVATARS_DIR", data / "avatars")
    monkeypatch.setattr(social, "AVATARS_DIR", data / "avatars")
    for mod in (manifest, main, catalogue, social, covers, slugrename):
        monkeypatch.setattr(mod, "PROJECTS_DIR", tmp_path)
    db.init_db()
    return TestClient(main.app), tmp_path


def _make_album(c, *, artist="Twenty One Pilots", title="Live", date=""):
    payload = {"album": {"artist": artist, "title": title, "date": date},
               "tracks": [{"n": 1, "title": "Song", "start": 0.0, "end": 3.0,
                           "locked": True}],
               "target": "data_disc"}
    return c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]


def _seed_social_rows(slug):
    """Une ligne dans chaque table clée par slug."""
    from backend.db import get_conn
    now = "2026-01-01T00:00:00+00:00"
    with get_conn() as conn:
        conn.execute("INSERT INTO favorites(username, slug, created_at) VALUES(?,?,?)",
                     ("alice", slug, now))
        conn.execute("INSERT INTO comments(slug, username, body, created_at) "
                     "VALUES(?,?,?,?)", (slug, "alice", "top", now))
        conn.execute("INSERT INTO covers(slug, username, file_key, cover_ext, "
                     "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                     (slug, "alice", "k1", ".png", now, now))
        conn.execute("INSERT INTO track_covers(slug, track_n, username, file_key, "
                     "cover_ext, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                     (slug, 1, "alice", "k1", ".png", now, now))
        conn.execute("INSERT INTO notifications(username, type, slug, created_at) "
                     "VALUES(?,?,?,?)", ("alice", "new_post", slug, now))
        conn.execute("INSERT INTO post_announcements(slug, announced_at) VALUES(?,?)",
                     (slug, now))


def _count(slug):
    from backend.db import get_conn
    tables = ["favorites", "comments", "covers", "track_covers", "notifications",
              "post_announcements"]
    with get_conn() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) c FROM {t} WHERE slug=?",
                                (slug,)).fetchone()["c"] for t in tables}


def test_preview_and_rename_migrates_everything(client):
    c, projects = client
    # Slug créé sans date -> « ...-untitled ».
    old = _make_album(c, date="")
    assert old.endswith("untitled")
    _seed_social_rows(old)

    # On complète la date : l'URL peut désormais être améliorée.
    c.put(f"/api/albums/{old}/meta", headers=GEST, json={
        "artist": "Twenty One Pilots", "title": "Live", "date": "2026-05-01",
        "venue": "", "festival": "", "city": "", "city_id": "",
        "artist_id": "", "festival_id": "", "guests": [],
        "source_url": "", "source_label": ""})

    prev = c.get(f"/api/albums/{old}/url-preview", headers=GEST).json()
    assert prev["changed"] is True
    new = prev["target"]
    assert new == "twenty-one-pilots-2026-05-01"

    res = c.post(f"/api/albums/{old}/rename-url", headers=GEST).json()
    assert res["ok"] and res["changed"] and res["slug"] == new

    # Dossier renommé.
    assert not (projects / old).exists()
    assert (projects / new / "manifest.yaml").exists()

    # Toutes les tables migrées : plus rien sous l'ancien slug, tout sous le neuf.
    assert _count(old) == {t: 0 for t in _count(old)}
    assert all(v == 1 for v in _count(new).values())

    # Ancienne URL -> redirection 301 vers la nouvelle.
    r = c.get(f"/album/{old}", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == f"/album/{new}"

    # Rejouer ne change plus rien (déjà propre).
    again = c.post(f"/api/albums/{new}/rename-url", headers=GEST).json()
    assert again["changed"] is False and again["reason"] == "already_clean"


def test_rename_chain_compresses_aliases(client):
    c, projects = client
    old = _make_album(c, artist="A", date="")          # a-untitled
    c.put(f"/api/albums/{old}/meta", headers=GEST, json={
        "artist": "A", "title": "T", "date": "2020-01-01", "venue": "",
        "festival": "", "city": "", "city_id": "", "artist_id": "",
        "festival_id": "", "guests": [], "source_url": "", "source_label": ""})
    s1 = c.post(f"/api/albums/{old}/rename-url", headers=GEST).json()["slug"]
    # Nouvelle date -> nouveau renommage : l'alias initial doit pointer vers le final.
    c.put(f"/api/albums/{s1}/meta", headers=GEST, json={
        "artist": "A", "title": "T", "date": "2021-02-02", "venue": "",
        "festival": "", "city": "", "city_id": "", "artist_id": "",
        "festival_id": "", "guests": [], "source_url": "", "source_label": ""})
    s2 = c.post(f"/api/albums/{s1}/rename-url", headers=GEST).json()["slug"]
    assert s2 != s1
    # Les deux anciennes URLs redirigent vers la dernière.
    for dead in (old, s1):
        r = c.get(f"/album/{dead}", follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == f"/album/{s2}"
