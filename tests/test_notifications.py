"""Notifications in-app : fan-out à la publication, préférences, centre, idempotence."""
import pytest
from fastapi.testclient import TestClient

GEST = {"X-authentik-username": "mod", "X-authentik-groups": "live2mp3-gestionnaire"}


def H(name):
    return {"X-authentik-username": name, "X-authentik-groups": "live2mp3-user"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend import catalogue, covers, db, main, manifest, notifications, social, suggest
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "live2mp3.db")
    monkeypatch.setattr(db, "AVATARS_DIR", data / "avatars")
    monkeypatch.setattr(social, "AVATARS_DIR", data / "avatars")
    for mod in (manifest, main, catalogue, social, notifications):
        monkeypatch.setattr(mod, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(covers, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(suggest, "resolve_artist",
                        lambda i: {"id": i, "label": "Deezer " + i, "picture": ""})
    db.init_db()
    return TestClient(main.app)


def _make_draft(c, artist="Coldplay", artist_id="892", festival="Glastonbury",
                guests=None, title="Live at Glastonbury"):
    payload = {"album": {"artist": artist, "title": title, "date": "2024-06-30"},
               "tracks": [{"n": 1, "title": "Song", "start": 0.0, "end": 3.0, "locked": True}],
               "target": "data_disc"}
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    c.put(f"/api/albums/{slug}/meta", headers=GEST, json={
        "artist": artist, "artist_id": artist_id, "title": title, "date": "2024-06-30",
        "festival": festival, "guests": guests or [],
    })
    return slug


def _publish(c, slug):
    return c.patch(f"/api/albums/{slug}/published", json={"published": True}, headers=GEST).json()


# --- Préférences -----------------------------------------------------------
def test_prefs_defaults_and_update(client):
    c = client
    d = c.get("/api/social/notif-prefs", headers=H("alice")).json()
    assert d["email_enabled"] is False           # master-switch off en test
    assert d["prefs"]["new_post:artist"] == {"inapp": True, "email": True}
    c.put("/api/social/notif-prefs", headers=H("alice"), json={
        "prefs": {"new_post:artist": {"inapp": False, "email": True}}})
    d = c.get("/api/social/notif-prefs", headers=H("alice")).json()
    assert d["prefs"]["new_post:artist"]["inapp"] is False


# --- Fan-out ---------------------------------------------------------------
def test_announce_fanout_dedupe_bell_and_prefs(client):
    c = client
    slug = _make_draft(c, guests=[{"id": "27", "name": "Beyonce"}])

    # Abonnés variés, tous AVANT la publication.
    c.post("/api/social/follow", headers=H("alice"), json={"target_type": "artist", "target_id": "892"})
    c.post("/api/social/follow", headers=H("bob"), json={"target_type": "festival", "target_id": "glastonbury"})
    c.post("/api/social/follow", headers=H("carol"), json={"target_type": "user", "target_id": "mod"})
    c.post("/api/social/follow", headers=H("grace"), json={"target_type": "artist", "target_id": "27"})  # invité
    # dave suit artiste ET festival → une seule notif (dédup).
    c.post("/api/social/follow", headers=H("dave"), json={"target_type": "artist", "target_id": "892"})
    c.post("/api/social/follow", headers=H("dave"), json={"target_type": "festival", "target_id": "glastonbury"})
    # erin suit mais cloche coupée → rien.
    c.post("/api/social/follow", headers=H("erin"), json={"target_type": "artist", "target_id": "892"})
    c.patch("/api/social/follow/notify", headers=H("erin"),
            json={"target_type": "artist", "target_id": "892", "notify": False})
    # frank suit mais a désactivé la catégorie artiste → rien.
    c.post("/api/social/follow", headers=H("frank"), json={"target_type": "artist", "target_id": "892"})
    c.put("/api/social/notif-prefs", headers=H("frank"),
          json={"prefs": {"new_post:artist": {"inapp": False, "email": True}}})

    r = _publish(c, slug)
    assert r["notified"] == 5     # alice, bob, carol, grace, dave (pas erin/frank/mod)

    def unread(name):
        return c.get("/api/social/notifications/count", headers=H(name)).json()["unread"]
    assert unread("alice") == 1
    assert unread("bob") == 1
    assert unread("carol") == 1
    assert unread("grace") == 1
    assert unread("dave") == 1     # dédup : pas 2
    assert unread("erin") == 0     # cloche coupée
    assert unread("frank") == 0    # catégorie désactivée
    assert unread("mod") == 0      # l'auteur ne se notifie pas

    # La raison est portée par la notif (artiste vs festival vs utilisateur).
    it = c.get("/api/social/notifications", headers=H("alice")).json()["items"][0]
    assert it["reason_type"] == "artist" and it["slug"] == slug and it["title"] == "Live at Glastonbury"
    assert c.get("/api/social/notifications", headers=H("carol")).json()["items"][0]["reason_type"] == "user"


def test_announce_idempotent(client):
    c = client
    slug = _make_draft(c)
    c.post("/api/social/follow", headers=H("alice"), json={"target_type": "artist", "target_id": "892"})
    assert _publish(c, slug)["notified"] == 1
    # Re-dé/publier ne renotifie pas.
    c.patch(f"/api/albums/{slug}/published", json={"published": False}, headers=GEST)
    assert _publish(c, slug)["notified"] == 0
    assert c.get("/api/social/notifications/count", headers=H("alice")).json()["unread"] == 1


# --- Centre de notifications ----------------------------------------------
def test_list_and_mark_read(client):
    c = client
    slug = _make_draft(c)
    c.post("/api/social/follow", headers=H("alice"), json={"target_type": "artist", "target_id": "892"})
    _publish(c, slug)
    d = c.get("/api/social/notifications", headers=H("alice")).json()
    assert d["unread"] == 1 and d["total"] == 1 and d["items"][0]["read"] is False
    r = c.post("/api/social/notifications/read", headers=H("alice"), json={"all": True}).json()
    assert r["unread"] == 0
    assert c.get("/api/social/notifications", headers=H("alice")).json()["items"][0]["read"] is True


def test_anon_no_notifications(client):
    d = client.get("/api/social/notifications").json()
    assert d["authenticated"] is False and d["unread"] == 0


# --- Idempotence / seed ----------------------------------------------------
def test_ensure_seeded_prevents_retroactive(client):
    from backend import notifications
    c = client
    slug = _make_draft(c)
    _publish(c, slug)                    # publie (et annonce) avant seed
    # Un abonné tardif + reseed : republier ne doit pas renotifier (déjà annoncé).
    c.post("/api/social/follow", headers=H("late"), json={"target_type": "artist", "target_id": "892"})
    notifications.ensure_seeded()        # marque les publiés comme annoncés
    c.patch(f"/api/albums/{slug}/published", json={"published": False}, headers=GEST)
    assert _publish(c, slug)["notified"] == 0
    assert c.get("/api/social/notifications/count", headers=H("late")).json()["unread"] == 0
