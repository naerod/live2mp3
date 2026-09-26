"""Pochettes multiples : import, crédit, likes, classement, tray card liée."""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

GEST = {"X-authentik-username": "mod", "X-authentik-groups": "live2mp3-gestionnaire"}
USER = {"X-authentik-username": "alice", "X-authentik-groups": "live2mp3-user"}
USER2 = {"X-authentik-username": "bob", "X-authentik-groups": "live2mp3-user"}
USER3 = {"X-authentik-username": "carol", "X-authentik-groups": "live2mp3-user"}


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


def _album(c, artist="A", title="Live", date="2026-01-01") -> str:
    payload = {"album": {"artist": artist, "title": title, "date": date},
               "tracks": [{"n": 1, "title": "Song", "start": 0.0, "end": 3.0, "locked": True}],
               "target": "data_disc"}
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    # L'outil crée des brouillons ; ces tests portent sur des albums publics.
    c.patch(f"/api/albums/{slug}/published", json={"published": True}, headers=GEST)
    return slug


def _png(color=(255, 0, 0)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, "PNG")
    return buf.getvalue()


def _pdf() -> bytes:
    return b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF"


def _post_cover(c, slug, headers, traycard=False, color=(255, 0, 0)):
    # Depuis 26edc0c, la tray card importée est une image (le PDF imprimable
    # est généré à la volée au téléchargement).
    files = {"cover": ("c.png", _png(color), "image/png")}
    if traycard:
        files["traycard"] = ("t.png", _png((0, 128, 255)), "image/png")
    return c.post(f"/api/social/albums/{slug}/covers", files=files, headers=headers)


# --- Import et crédit ------------------------------------------------------
def test_upload_requires_auth(client):
    c, _ = client
    slug = _album(c)
    assert _post_cover(c, slug, {}).status_code == 401


def test_any_user_can_upload_and_is_credited(client):
    """Tout compte connecté peut proposer, pas seulement les gestionnaires."""
    c, _ = client
    slug = _album(c)
    r = _post_cover(c, slug, USER)
    assert r.status_code == 200
    covers = r.json()["covers"]
    assert len(covers) == 1
    assert covers[0]["username"] == "alice"
    assert covers[0]["rank"] == 1


def test_upload_rejects_non_image(client):
    c, _ = client
    slug = _album(c)
    r = c.post(f"/api/social/albums/{slug}/covers",
               files={"cover": ("x.png", b"pas une image", "image/png")}, headers=USER)
    assert r.status_code == 400


def test_cover_image_is_served(client):
    c, _ = client
    slug = _album(c)
    cid = _post_cover(c, slug, USER).json()["covers"][0]["id"]
    r = c.get(f"/cover-img/{cid}")
    assert r.status_code == 200 and r.content == _png()


# --- Likes et classement ---------------------------------------------------
def test_most_liked_cover_wins(client):
    c, _ = client
    slug = _album(c)
    a = _post_cover(c, slug, USER).json()["covers"][0]["id"]
    b = _post_cover(c, slug, USER2, color=(0, 255, 0)).json()["covers"][-1]["id"]
    # b prend deux likes, a un seul -> b passe en tête.
    c.post(f"/api/social/covers/{b}/like", headers=USER)
    c.post(f"/api/social/covers/{b}/like", headers=USER3)
    r = c.post(f"/api/social/covers/{a}/like", headers=USER2).json()
    assert [x["id"] for x in r["covers"]] == [b, a]
    assert r["covers"][0]["likes"] == 2


def test_like_toggles(client):
    c, _ = client
    slug = _album(c)
    cid = _post_cover(c, slug, USER).json()["covers"][0]["id"]
    r = c.post(f"/api/social/covers/{cid}/like", headers=USER2).json()
    assert r["covers"][0]["likes"] == 1 and r["covers"][0]["liked"] is True
    r = c.post(f"/api/social/covers/{cid}/like", headers=USER2).json()
    assert r["covers"][0]["likes"] == 0 and r["covers"][0]["liked"] is False


def test_pinned_cover_beats_likes(client):
    """Une épinglée passe devant, même battue aux likes."""
    c, _ = client
    slug = _album(c)
    a = _post_cover(c, slug, USER).json()["covers"][0]["id"]
    b = _post_cover(c, slug, USER2, color=(0, 255, 0)).json()["covers"][-1]["id"]
    c.post(f"/api/social/covers/{b}/like", headers=USER)
    c.post(f"/api/social/covers/{b}/like", headers=USER3)
    assert c.get(f"/api/social/albums/{slug}/covers").json()["covers"][0]["id"] == b
    r = c.post(f"/api/social/covers/{a}/pin", headers=GEST).json()
    assert [x["id"] for x in r["covers"]] == [a, b]
    assert r["covers"][0]["pinned"] is True


def _insert_auto_cover(slug, username="mod"):
    """Simule la miniature récupérée à l'import par lien (auto=1), sans réseau."""
    from uuid import uuid4
    from backend import covers as cov
    from backend.db import get_conn
    key = uuid4().hex
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO covers(slug, username, file_key, cover_ext, traycard_ext, "
            "caption, auto, created_at, updated_at) "
            "VALUES(?,?,?,?,'','',1,'2000-01-01T00:00:00+00:00','2000-01-01T00:00:00+00:00')",
            (slug, username, key, ".png"),
        )
        cid = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        cov.covers_dir(slug).mkdir(parents=True, exist_ok=True)
        cov.cover_file(slug, key, ".png").write_bytes(_png((10, 10, 10)))
    cov._on_covers_changed(slug)
    return cid


def test_manual_cover_beats_auto_thumbnail(client):
    """Bug : une pochette auto (miniature d'import), plus ANCIENNE, l'emportait
    au départage `created_at`. Une pochette manuelle doit passer devant d'office,
    et le crédit « Pochette automatique » disparaître aussitôt."""
    c, _ = client
    slug = _album(c)
    auto = _insert_auto_cover(slug)
    # Tant qu'elle est seule, l'auto gagne et l'album est crédité « automatique ».
    assert c.get(f"/api/social/albums/{slug}/covers").json()["covers"][0]["id"] == auto
    assert c.get(f"/api/catalogue/{slug}", headers=GEST).json()["cover_auto"] is True
    # Upload manuel (auto=0) : postérieur, mais doit gagner malgré tout.
    manual = _post_cover(c, slug, USER, color=(0, 255, 0)).json()
    assert manual["covers"][0]["id"] != auto
    assert manual["covers"][0]["auto"] is False
    won = c.get(f"/api/social/albums/{slug}/covers").json()["covers"]
    assert won[0]["id"] != auto and won[0]["auto"] is False
    assert c.get(f"/api/catalogue/{slug}", headers=GEST).json()["cover_auto"] is False


def test_pin_is_exclusive_and_toggles(client):
    c, _ = client
    slug = _album(c)
    a = _post_cover(c, slug, USER).json()["covers"][0]["id"]
    b = _post_cover(c, slug, USER2, color=(0, 255, 0)).json()["covers"][-1]["id"]
    c.post(f"/api/social/covers/{a}/pin", headers=GEST)
    r = c.post(f"/api/social/covers/{b}/pin", headers=GEST).json()
    pinned = [x["id"] for x in r["covers"] if x["pinned"]]
    assert pinned == [b]                      # l'épingle a bougé, pas doublé
    r = c.post(f"/api/social/covers/{b}/pin", headers=GEST).json()
    assert [x["id"] for x in r["covers"] if x["pinned"]] == []   # bascule


def test_pin_requires_gestionnaire(client):
    c, _ = client
    slug = _album(c)
    cid = _post_cover(c, slug, USER).json()["covers"][0]["id"]
    assert c.post(f"/api/social/covers/{cid}/pin", headers=USER).status_code == 403


# --- Tray card liée --------------------------------------------------------
def test_traycard_uploaded_with_cover(client):
    c, _ = client
    slug = _album(c)
    cov = _post_cover(c, slug, USER, traycard=True).json()["covers"][0]
    assert cov["has_traycard"] is True
    assert c.get(f"/traycard-img/{cov['id']}").status_code == 200


def test_traycard_preview_is_served_inline(client):
    """L'aperçu en iframe doit être inline : en attachment, le navigateur
    ouvre un 'Enregistrer sous' au lieu d'afficher le PDF."""
    c, _ = client
    slug = _album(c)
    _post_cover(c, slug, USER, traycard=True)
    r = c.get(f"/app/traycard/{slug}", headers=GEST)
    assert r.status_code == 200
    assert "attachment" not in r.headers.get("content-disposition", "")
    assert "attachment" in c.get(f"/download/{slug}/traycard",
                                headers=USER).headers["content-disposition"]


def test_traycard_upload_accepts_images_only(client):
    """L'import de tray card est image-only depuis 26edc0c : le PDF imprimable
    est généré au téléchargement. `traycard_kind` reste exposé pour les tray
    cards PDF héritées de la migration."""
    c, _ = client
    slug = _album(c)
    cid = _post_cover(c, slug, USER, traycard=True).json()["covers"][0]["id"]
    assert c.get(f"/api/social/albums/{slug}/covers").json()["covers"][0]["traycard_kind"] == "image"
    r = c.put(f"/api/social/covers/{cid}/traycard",
              files={"traycard": ("t.pdf", _pdf(), "application/pdf")}, headers=USER)
    assert r.status_code == 400


def test_traycard_absent_by_default(client):
    c, _ = client
    slug = _album(c)
    cov = _post_cover(c, slug, USER).json()["covers"][0]
    assert cov["has_traycard"] is False and cov["traycard_url"] is None
    assert c.get(f"/traycard-img/{cov['id']}").status_code == 404


def test_traycard_attaches_to_existing_cover(client):
    c, _ = client
    slug = _album(c)
    cid = _post_cover(c, slug, USER).json()["covers"][0]["id"]
    r = c.put(f"/api/social/covers/{cid}/traycard",
              files={"traycard": ("t.png", _png((0, 128, 255)), "image/png")}, headers=USER)
    assert r.status_code == 200 and r.json()["covers"][0]["has_traycard"] is True


def test_traycard_not_editable_by_others(client):
    c, _ = client
    slug = _album(c)
    cid = _post_cover(c, slug, USER).json()["covers"][0]["id"]
    r = c.put(f"/api/social/covers/{cid}/traycard",
              files={"traycard": ("t.png", _png((0, 128, 255)), "image/png")}, headers=USER2)
    assert r.status_code == 403


def test_deleting_cover_removes_its_traycard(client):
    """La tray card ne survit pas à sa cover : elle n'existe que rattachée."""
    c, tmp = client
    slug = _album(c)
    cid = _post_cover(c, slug, USER, traycard=True).json()["covers"][0]["id"]
    assert c.delete(f"/api/social/covers/{cid}", headers=USER).status_code == 200
    assert c.get(f"/traycard-img/{cid}").status_code == 404
    assert not list((tmp / slug / "artwork" / "covers").glob("*_traycard*"))


def test_album_traycard_follows_winning_cover(client):
    c, _ = client
    slug = _album(c)
    _post_cover(c, slug, USER, traycard=True)          # gagnante, avec tray card
    assert c.get(f"/download/{slug}/traycard", headers=USER).status_code == 200
    # Une concurrente sans tray card prend la tête -> l'album n'en propose plus.
    b = _post_cover(c, slug, USER2, color=(0, 255, 0)).json()["covers"][-1]["id"]
    c.post(f"/api/social/covers/{b}/like", headers=USER)
    assert c.get(f"/download/{slug}/traycard", headers=USER).status_code == 404


# --- Suppression et permissions -------------------------------------------
def test_owner_can_delete_moderator_too(client):
    c, _ = client
    slug = _album(c)
    a = _post_cover(c, slug, USER).json()["covers"][0]["id"]
    assert c.delete(f"/api/social/covers/{a}", headers=USER2).status_code == 403
    assert c.delete(f"/api/social/covers/{a}", headers=GEST).status_code == 200


# --- Commentaires par cover ------------------------------------------------
def test_comments_are_scoped_per_cover(client):
    c, _ = client
    slug = _album(c)
    a = _post_cover(c, slug, USER).json()["covers"][0]["id"]
    b = _post_cover(c, slug, USER2, color=(0, 255, 0)).json()["covers"][-1]["id"]
    c.post(f"/api/social/albums/{slug}/comments",
           json={"body": "sur A", "cover_id": a}, headers=USER)
    c.post(f"/api/social/albums/{slug}/comments",
           json={"body": "sur l'album"}, headers=USER)
    ra = c.get(f"/api/social/albums/{slug}/comments", params={"cover_id": a}).json()
    rb = c.get(f"/api/social/albums/{slug}/comments", params={"cover_id": b}).json()
    ralbum = c.get(f"/api/social/albums/{slug}/comments").json()
    assert [x["body"] for x in ra["comments"]] == ["sur A"]
    assert rb["comments"] == []
    assert [x["body"] for x in ralbum["comments"]] == ["sur l'album"]


def test_cover_comment_count_exposed(client):
    c, _ = client
    slug = _album(c)
    a = _post_cover(c, slug, USER).json()["covers"][0]["id"]
    c.post(f"/api/social/albums/{slug}/comments",
           json={"body": "jolie", "cover_id": a}, headers=USER2)
    assert c.get(f"/api/social/albums/{slug}/covers").json()["covers"][0]["comments"] == 1


def test_reply_cannot_jump_thread(client):
    c, _ = client
    slug = _album(c)
    a = _post_cover(c, slug, USER).json()["covers"][0]["id"]
    root = c.post(f"/api/social/albums/{slug}/comments",
                  json={"body": "album"}, headers=USER).json()["id"]
    r = c.post(f"/api/social/albums/{slug}/comments",
               json={"body": "greffe", "parent_id": root, "cover_id": a}, headers=USER2)
    assert r.status_code == 400


# --- Nommage du zip --------------------------------------------------------
def test_zip_pairs_cover_and_traycard_alphabetically(client):
    """Tri alphabétique = ordre de popularité, chaque paire restant collée."""
    from backend import main
    c, tmp = client
    slug = _album(c)
    _post_cover(c, slug, USER, traycard=True)
    b = _post_cover(c, slug, USER2, traycard=True, color=(0, 255, 0)).json()["covers"][-1]["id"]
    c.post(f"/api/social/covers/{b}/like", headers=USER3)   # bob passe en tête

    names = [arc for _, arc in main._album_extras(tmp / slug)]
    assert sorted(names) == [
        "artwork/01-bob_cover.png",
        "artwork/01-bob_traycard.png",
        "artwork/02-alice_cover.png",
        "artwork/02-alice_traycard.png",
    ]


def test_zip_rebuilds_when_ranking_changes(client):
    """Un changement de classement renomme les entrées sans toucher les mtime."""
    from backend import main
    c, tmp = client
    slug = _album(c)
    a = _post_cover(c, slug, USER).json()["covers"][0]["id"]
    b = _post_cover(c, slug, USER2, color=(0, 255, 0)).json()["covers"][-1]["id"]
    audio = tmp / slug / "build" / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    (audio / "01. Song.mp3").write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 512)

    def arcnames():
        z = main._zip_media(tmp / slug, "mp3")
        with zipfile.ZipFile(z) as f:
            return sorted(n for n in f.namelist() if n.startswith("artwork/"))

    assert arcnames() == ["artwork/01-alice_cover.png", "artwork/02-bob_cover.png"]
    c.post(f"/api/social/covers/{b}/like", headers=USER3)
    assert arcnames() == ["artwork/01-bob_cover.png", "artwork/02-alice_cover.png"]


# --- Migration legacy ------------------------------------------------------
def test_migration_credits_importer_and_is_idempotent(client):
    from backend import migrate_covers
    from backend.manifest import Manifest
    c, tmp = client
    slug = _album(c)
    art = tmp / slug / "artwork"
    art.mkdir(parents=True, exist_ok=True)
    (art / "cover.jpg").write_bytes(_png())
    (art / "tray_card.pdf").write_bytes(_pdf())
    mpath = tmp / slug / "manifest.yaml"
    m = Manifest.load(mpath)
    m.data["album"]["cover"] = "artwork/cover.jpg"
    m.data.setdefault("meta", {})["imported_by"] = "nathan"
    m.save()

    assert migrate_covers.migrate() == 1
    assert migrate_covers.migrate() == 0            # idempotent

    covers = c.get(f"/api/social/albums/{slug}/covers").json()["covers"]
    assert len(covers) == 1
    assert covers[0]["username"] == "nathan"
    assert covers[0]["has_traycard"] is True
    # L'original reste en place : la migration copie, elle ne déplace pas.
    assert (art / "cover.jpg").exists()
    assert Manifest.load(mpath).data["album"]["cover"] == "artwork/covers/legacy_cover.jpg"


# --- Pochettes par piste ---------------------------------------------------
def test_track_cover_upload_requires_gestionnaire(client):
    c, _ = client
    slug = _album(c)
    files = {"file": ("c.png", _png(), "image/png")}
    assert c.post(f"/api/albums/{slug}/tracks/1/cover", files=files, headers=USER).status_code == 403
    assert c.post(f"/api/albums/{slug}/tracks/1/cover", files=files, headers={}).status_code == 401


def test_track_cover_upload_and_serve(client):
    c, _ = client
    slug = _album(c)
    files = {"file": ("c.png", _png((0, 200, 0)), "image/png")}
    r = c.post(f"/api/albums/{slug}/tracks/1/cover", files=files, headers=GEST)
    assert r.status_code == 200
    d = r.json()
    assert d["ok"]
    assert len(d["track_covers"]) == 1
    assert d["track_covers"][0]["track_n"] == 1
    # serve
    tc_id = d["track_covers"][0]["id"]
    img = c.get(f"/track-cover/{tc_id}")
    assert img.status_code == 200
    assert img.headers["content-type"].startswith("image/")


def test_track_cover_replace(client):
    c, _ = client
    slug = _album(c)
    files1 = {"file": ("c.png", _png((255, 0, 0)), "image/png")}
    r1 = c.post(f"/api/albums/{slug}/tracks/1/cover", files=files1, headers=GEST)
    id1 = r1.json()["track_covers"][0]["id"]
    files2 = {"file": ("c.png", _png((0, 0, 255)), "image/png")}
    r2 = c.post(f"/api/albums/{slug}/tracks/1/cover", files=files2, headers=GEST)
    assert r2.status_code == 200
    # still 1 entry (upsert)
    assert len(r2.json()["track_covers"]) == 1


def test_track_cover_delete(client):
    c, _ = client
    slug = _album(c)
    files = {"file": ("c.png", _png(), "image/png")}
    r = c.post(f"/api/albums/{slug}/tracks/1/cover", files=files, headers=GEST)
    tc_id = r.json()["track_covers"][0]["id"]
    d = c.delete(f"/api/track-covers/{tc_id}", headers=GEST)
    assert d.status_code == 200
    assert d.json()["track_covers"] == []
    assert c.get(f"/track-cover/{tc_id}").status_code == 404


def test_per_track_covers_flag(client):
    c, _ = client
    slug = _album(c)
    # default false
    d = c.get(f"/api/albums/{slug}", headers=GEST).json()
    assert d["per_track_covers"] is False
    # toggle on
    r = c.patch(f"/api/albums/{slug}/per-track-covers",
                json={"per_track_covers": True}, headers=GEST)
    assert r.status_code == 200
    assert r.json()["per_track_covers"] is True
    # reflected in album detail
    assert c.get(f"/api/albums/{slug}", headers=GEST).json()["per_track_covers"] is True
    # catalogue detail (public)
    assert c.get(f"/api/catalogue/{slug}").json()["per_track_covers"] is True


def test_track_covers_in_catalogue(client):
    c, _ = client
    slug = _album(c)
    files = {"file": ("c.png", _png((100, 200, 50)), "image/png")}
    c.post(f"/api/albums/{slug}/tracks/1/cover", files=files, headers=GEST)
    d = c.get(f"/api/catalogue/{slug}").json()
    assert len(d["track_covers"]) == 1
    assert d["track_covers"][0]["track_n"] == 1
    assert "/track-cover/" in d["track_covers"][0]["cover_url"]


# --- Propagation vers Jellyfin/Finamp sur changement de gagnante -----------
def test_cover_change_propagates_to_media_and_jellyfin(client, monkeypatch):
    """Album publié + rendu : un changement de gagnante répercute vers le média
    servi à Jellyfin (APIC des pistes pour album à pochette unique) et force un
    refresh Jellyfin ciblé."""
    from backend import albumfiles, covers, jellyfin
    c, root = client
    slug = _album(c)
    # simule un album déjà rendu
    (root / slug / "build" / "audio").mkdir(parents=True)

    embedded = []
    refreshed = []
    monkeypatch.setattr(albumfiles, "_write_album_cover",
                        lambda s, m: embedded.append(s) or 1)
    monkeypatch.setattr(jellyfin, "refresh_album",
                        lambda s, **kw: refreshed.append(s) or True)

    _post_cover(c, slug, USER)  # 1re gagnante -> changement réel

    assert embedded == [slug]
    assert refreshed == [slug]


def test_per_track_album_propagates_folder_cover_only(client, monkeypatch):
    """Album `per_track_covers` : on dépose l'image de dossier sans jamais
    ré-embarquer la pochette d'album dans les pistes (elles ont la leur)."""
    from backend import albumfiles, covers, jellyfin
    c, root = client
    slug = _album(c)
    c.patch(f"/api/albums/{slug}/per-track-covers",
            json={"per_track_covers": True}, headers=GEST)
    (root / slug / "build" / "audio").mkdir(parents=True)

    called = {"album": 0, "folder": 0}
    monkeypatch.setattr(albumfiles, "_write_album_cover",
                        lambda s, m: called.__setitem__("album", called["album"] + 1))
    monkeypatch.setattr(albumfiles, "_write_folder_cover",
                        lambda s, m: called.__setitem__("folder", called["folder"] + 1))
    monkeypatch.setattr(jellyfin, "refresh_album", lambda s, **kw: True)

    _post_cover(c, slug, USER)

    assert called["folder"] == 1
    assert called["album"] == 0


def test_unpublished_album_does_not_touch_media(client, monkeypatch):
    """Brouillon : aucune propagation média/Jellyfin (l'album n'est pas monté)."""
    from backend import albumfiles, jellyfin
    c, root = client
    # album non publié
    payload = {"album": {"artist": "Z", "title": "Draft", "date": "2026-01-01"},
               "tracks": [{"n": 1, "title": "S", "start": 0.0, "end": 3.0, "locked": True}],
               "target": "data_disc"}
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    (root / slug / "build" / "audio").mkdir(parents=True)

    hits = []
    monkeypatch.setattr(albumfiles, "_write_album_cover", lambda s, m: hits.append(s))
    monkeypatch.setattr(jellyfin, "refresh_album", lambda s, **kw: hits.append(s))

    _post_cover(c, slug, USER)
    assert hits == []


def test_reorder_tracks_renumbers_n(client, monkeypatch):
    """Réordonner la tracklist renumérote `n` par position.

    Régression coldplay-untitled : la fiche métadonnées réordonnait la liste
    mais gardait l'ancien `n`, alors que fichiers et tags suivaient déjà la
    position — la fiche publique affichait donc des numéros désordonnés
    (02,03,…,01). Le `n` doit refléter la position finale.
    """
    from backend import jellyfin
    monkeypatch.setattr(jellyfin, "refresh_album", lambda s, **kw: None)
    c, root = client
    payload = {"album": {"artist": "Coldplay", "title": "Live", "date": "2026-01-01"},
               "tracks": [
                   {"n": 1, "title": "First", "start": 0.0, "end": 3.0, "locked": True},
                   {"n": 2, "title": "Second", "start": 3.0, "end": 6.0, "locked": True},
                   {"n": 3, "title": "Third", "start": 6.0, "end": 9.0, "locked": True},
               ],
               "target": "data_disc"}
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    c.patch(f"/api/albums/{slug}/published", json={"published": True}, headers=GEST)
    (root / slug / "build" / "audio").mkdir(parents=True, exist_ok=True)

    # Nouvel ordre : Third, First, Second (on envoie les `n` d'origine).
    r = c.put(f"/api/albums/{slug}/tracks", headers=GEST, json={"tracks": [
        {"n": 3, "title": "Third"},
        {"n": 1, "title": "First"},
        {"n": 2, "title": "Second"},
    ]})
    assert r.status_code == 200

    tracks = c.get(f"/api/catalogue/{slug}", headers=GEST).json()["tracks"]
    assert [(t["n"], t["title"]) for t in tracks] == [
        (1, "Third"), (2, "First"), (3, "Second")]


# --- Image « Disc » Jellyfin ----------------------------------------------
def test_folder_cover_also_writes_disc_image(client, monkeypatch):
    """La pochette carrée devient aussi l'image « Disc » de Jellyfin.

    Jellyfin lit `cover.jpg` (Primary) et `disc.<ext>` (Disc) dans le dossier
    de l'album ; c'est ce qui rend la pochette « disque » automatique.
    """
    from backend import albumfiles, jellyfin, manifest
    c, root = client
    slug = _album(c)
    audio = root / slug / "build" / "audio"
    audio.mkdir(parents=True)
    monkeypatch.setattr(albumfiles, "_projects_dir", lambda: root)
    monkeypatch.setattr(jellyfin, "refresh_album", lambda s, **kw: True)
    art = root / slug / "artwork"
    art.mkdir(parents=True, exist_ok=True)
    (art / "cover.png").write_bytes(_png())
    m = manifest.Manifest.load(root / slug / "manifest.yaml")
    m.data.setdefault("album", {})["cover"] = "artwork/cover.png"
    m.save()

    assert albumfiles._write_folder_cover(slug, m) is True
    assert (audio / "cover.jpg").exists()
    assert (audio / "disc.png").exists()

    # Pochette retirée → l'image « Disc » disparaît (pas d'image fantôme).
    m.data["album"].pop("cover")
    m.save()
    albumfiles._write_disc_cover(slug, m)
    assert not list(audio.glob("disc.*"))


def test_manage_page_sees_cover_of_unpublished_album(client):
    """Régression 2026-09-10 (gazo-2025-09-07) : la page de gestion d'un album
    dépublié affichait « Aucune pochette » alors qu'une pochette existait —
    /api/albums/{slug} lisait le catalogue public, sans les brouillons."""
    c, root = client
    slug = _album(c)
    mp3 = root / slug / "build" / "audio"
    mp3.mkdir(parents=True, exist_ok=True)
    (mp3 / "01.mp3").write_bytes(b"ID3")
    assert _post_cover(c, slug, USER, traycard=True).status_code == 200
    c.patch(f"/api/albums/{slug}/published", json={"published": False}, headers=GEST)
    d = c.get(f"/api/albums/{slug}", headers=GEST).json()
    assert d["has_cover"] is True
    assert d["has_traycard"] is True
    assert d["has_mp3"] is True


def test_manifest_cover_materialisee_en_ligne_covers(client):
    """Un album dont la pochette ne vit que dans le manifest (cas des albums
    importés dans un autre environnement : projets partagés, base sociale
    scindée) doit exposer une VRAIE ligne `covers` à la première lecture.

    Sans elle la fiche retombait sur une pseudo-pochette `id:0` : pas de
    likes, pas de commentaires, pas d'épinglage, et le clic n'ouvrait que
    l'agrandissement au lieu de la fiche pochette.
    """
    c, projects = client
    slug = _album(c)
    art = projects / slug / "artwork"
    art.mkdir(parents=True, exist_ok=True)
    (art / "cover.png").write_bytes(_png())
    from backend.manifest import Manifest
    mpath = projects / slug / "manifest.yaml"
    m = Manifest.load(mpath)
    m.data.setdefault("album", {})["cover"] = "artwork/cover.png"
    m.save()

    covers = c.get(f"/api/social/albums/{slug}/covers", headers=USER).json()["covers"]
    assert len(covers) == 1
    win = covers[0]
    assert win["id"] > 0            # un vrai id → fiche pochette, likes, pin
    assert win["auto"] is True      # un import manuel doit passer devant
    # L'image est bien servie par le pipeline social.
    assert c.get(f"/cover-img/{win['id']}").status_code == 200

    # Idempotent : relire ne crée pas de doublon.
    again = c.get(f"/api/social/albums/{slug}/covers", headers=USER).json()["covers"]
    assert [x["id"] for x in again] == [win["id"]]

    # Le manifest n'est PAS repointé : materialiser n'est pas changer de
    # pochette (repointer ré-embarquerait les APIC sur le volume partagé).
    assert Manifest.load(mpath).data["album"]["cover"] == "artwork/cover.png"

    # Un import manuel passe devant la pochette auto.
    r = c.post(f"/api/social/albums/{slug}/covers",
               files={"cover": ("c.png", _png((0, 255, 0)), "image/png")}, headers=USER)
    assert r.status_code == 200
    assert r.json()["covers"][0]["auto"] is False


def test_zip_des_visuels(client):
    """« Tout télécharger » : un ZIP des visuels gagnants, construit sur disque."""
    c, projects = client
    slug = _album(c)
    c.post(f"/api/social/albums/{slug}/covers",
           files={"cover": ("c.png", _png(), "image/png")}, headers=USER)
    c.post(f"/api/social/albums/{slug}/images/banner",
           files={"cover": ("b.png", _png((0, 0, 255)), "image/png")}, headers=USER)

    r = c.get(f"/download/{slug}/artwork", headers=USER)
    assert r.status_code == 200
    names = sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert names == [f"{slug}-banner.png", f"{slug}-cover.png"]

    # Sans aucun visuel : 404 plutôt qu'une archive vide.
    other = _album(c, artist="B", title="Vide", date="2026-02-02")
    assert c.get(f"/download/{other}/artwork", headers=USER).status_code == 404

    # Anonyme : refusé comme les autres téléchargements.
    assert c.get(f"/download/{slug}/artwork").status_code in (401, 403)


def test_vitrine_sert_la_gagnante_en_base_pas_le_manifest(client):
    """`/cover/{slug}` (vitrine) et la fiche doivent montrer la même image,
    même si le manifest partagé pointe ailleurs (réécrit par l'autre
    environnement). Incident 2026-09-26 : Falling In Reverse."""
    c, projects = client
    slug = _album(c)
    r = _post_cover(c, slug, USER, color=(0, 0, 255))
    win = r.json()["covers"][0]
    # L'autre environnement repointe le manifest partagé sur une autre image.
    art = projects / slug / "artwork"
    (art / "autre.png").write_bytes(_png((0, 255, 0)))
    from backend.manifest import Manifest
    mpath = projects / slug / "manifest.yaml"
    m = Manifest.load(mpath)
    m.data["album"]["cover"] = "artwork/autre.png"
    m.save()
    assert (c.get(f"/cover/{slug}", headers=USER).content
            == c.get(f"/cover-img/{win['id']}").content)


def test_preprod_ne_reecrit_pas_le_manifest_partage(client, monkeypatch):
    """Un album de prod n'est jamais repointé depuis la preprod."""
    c, projects = client
    slug = _album(c)
    from backend import covers
    from backend.manifest import Manifest
    mpath = projects / slug / "manifest.yaml"
    before = Manifest.load(mpath).data.get("album", {}).get("cover")
    monkeypatch.setattr(covers, "APP_ENV", "preprod")
    _post_cover(c, slug, USER)
    assert Manifest.load(mpath).data.get("album", {}).get("cover") == before
    # Un album créé en preprod reste, lui, piloté par la preprod.
    m = Manifest.load(mpath)
    m.data["origin_env"] = "preprod"
    m.save()
    _post_cover(c, slug, USER, color=(0, 255, 0))
    assert Manifest.load(mpath).data["album"]["cover"].startswith("artwork/covers/")
