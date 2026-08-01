"""T9 — API : création job -> pipeline sur fixture -> états SSE."""
import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


GEST = {"X-authentik-username": "g", "X-authentik-groups": "live2mp3-gestionnaire"}
USER = {"X-authentik-username": "u", "X-authentik-groups": "live2mp3-user"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Le zip complet embarque les pochettes, dont le classement vit en base
    # sociale : le téléchargement en dépend, donc on l'isole aussi ici.
    # `PROJECTS_DIR` est patché module par module (chacun l'a importé par
    # valeur, un patch sur `manifest` seul ne les atteindrait pas).
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


def _seed_master(project_dir: Path):
    (project_dir / "source").mkdir(parents=True, exist_ok=True)
    freqs = [220, 330, 440, 550]
    fc = ";".join(f"sine=frequency={f}:duration=3[a{i}]"
                  for i, f in enumerate(freqs))
    concat = "".join(f"[a{i}]" for i in range(len(freqs)))
    fc += f";{concat}concat=n={len(freqs)}:v=0:a=1[out]"
    subprocess.run([
        "ffmpeg", "-y", "-filter_complex", fc, "-map", "[out]",
        "-ar", "44100", "-ac", "2", str(project_dir / "source" / "master.wav"),
    ], check=True, capture_output=True)


def test_create_job_and_pipeline(client):
    c, projects = client
    payload = {
        "album": {"artist": "Test Artist", "title": "Live Album",
                  "date": "2026-01-01", "venue": "Somewhere"},
        "tracks": [
            {"n": 1, "title": "A", "start": 0.0, "end": 3.0, "locked": True},
            {"n": 2, "title": "B", "start": 3.0, "end": 6.0, "locked": True},
            {"n": 3, "title": "C", "start": 6.0, "end": 9.0, "locked": True},
            {"n": 4, "title": "D", "start": 9.0, "end": 12.0, "locked": True},
        ],
        "target": "data_disc",
    }
    r = c.post("/api/jobs", json=payload, headers=GEST)
    assert r.status_code == 200
    slug = r.json()["slug"]
    assert slug == "test-artist-2026-01-01"

    _seed_master(projects / slug)

    # manifest lisible via API
    m = c.get(f"/api/jobs/{slug}/manifest", headers=GEST).json()
    assert len(m["tracks"]) == 4

    # lancement render + collecte SSE
    r = c.post(f"/api/jobs/{slug}/render", params={"media": "audio"}, headers=GEST)
    assert r.status_code == 200

    with c.stream("GET", f"/api/jobs/{slug}/events", headers=GEST) as resp:
        stages_done = set()
        completed = False
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            ev = json.loads(line[5:].strip())
            if ev["status"] == "done":
                stages_done.add(ev["stage"])
            if ev["status"] == "complete":
                completed = True
                break
            if ev["status"] == "error":
                pytest.fail(f"pipeline error: {ev['info']}")
    assert completed
    # `bundle` ne fait plus partie du pipeline : le ZIP est assemblé à la
    # demande au téléchargement et supprimé ensuite (13 Go de doublons relevés
    # le 2026-07-30). Il est couvert par test_bulk_download_auth_and_zip.
    assert {"render", "tags", "artwork", "disc"} <= stages_done


def test_markers_update_locks(client):
    c, projects = client
    payload = {
        "album": {"artist": "X", "title": "Y", "date": "2026-02-02"},
        "tracks": [{"n": 1, "title": "A"}, {"n": 2, "title": "B"}],
        "target": "audio_cd",
    }
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    r = c.put(f"/api/jobs/{slug}/markers", headers=GEST, json={
        "tracks": {"1": {"start": 0.0, "end": 10.0},
                   "2": {"start": 10.0, "end": 20.0}}, "lock": True})
    assert r.status_code == 200
    m = c.get(f"/api/jobs/{slug}/manifest", headers=GEST).json()
    assert m["tracks"][0]["locked"] is True
    assert m["tracks"][0]["start"] == 0.0


def _wait_render_complete(c, slug):
    with c.stream("GET", f"/api/jobs/{slug}/events", headers=GEST) as resp:
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            ev = json.loads(line[5:].strip())
            if ev["status"] == "error":
                pytest.fail(f"pipeline error: {ev['info']}")
            if ev["status"] == "complete":
                break


def test_reedit_published_album_forces_rerender_and_refreshes_jellyfin(client, monkeypatch):
    """Rouvrir l'éditeur sur un album déjà publié (bouton « Ouvrir l'éditeur
    audio » de la fiche de gestion) : le re-rendu doit refléter le nouveau
    découpage même à nom de fichier inchangé, purger les pistes retirées, et
    déclencher un rafraîchissement Jellyfin — ce qu'un rendu de création
    (album encore non publié) ne doit pas faire."""
    from backend import jellyfin

    calls = []
    monkeypatch.setattr(jellyfin, "refresh_library", lambda: calls.append(1) or True)

    c, projects = client
    payload = {
        "album": {"artist": "Re", "title": "Edit", "date": "2026-04-04"},
        "tracks": [
            {"n": 1, "title": "A", "start": 0.0, "end": 3.0, "locked": True},
            {"n": 2, "title": "B", "start": 3.0, "end": 6.0, "locked": True},
        ],
        "target": "audio_cd",
    }
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    _seed_master(projects / slug)

    # Rendu de création (album non publié) : pas de refresh Jellyfin.
    c.post(f"/api/jobs/{slug}/render", params={"media": "audio"}, headers=GEST)
    _wait_render_complete(c, slug)
    assert calls == []
    audio_dir = projects / slug / "build" / "audio"
    track1_mp3 = next(audio_dir.glob("01.*"))
    track1_mtime = track1_mp3.stat().st_mtime
    track2_mp3 = next(audio_dir.glob("02.*"))

    # Publication, puis ré-édition (fusion des 2 pistes en 1 seule "A" plus
    # longue : nom de fichier inchangé pour la piste 1, piste 2 supprimée).
    c.patch(f"/api/albums/{slug}/published", json={"published": True}, headers=GEST)
    r = c.put(f"/api/jobs/{slug}/setlist", headers=GEST,
             json={"tracks": [{"n": 1, "title": "A", "start": 0.0, "end": 6.0}]})
    assert r.status_code == 200

    c.post(f"/api/jobs/{slug}/render", params={"media": "audio"}, headers=GEST)
    _wait_render_complete(c, slug)

    # Refresh Jellyfin déclenché.
    assert calls == [1]
    # L'ancienne piste 2 (orpheline) a disparu ; la piste 1 (nom de fichier
    # inchangé) a bien été réencodée malgré l'idempotence par défaut.
    remaining = list(audio_dir.glob("*.mp3"))
    assert len(remaining) == 1
    assert not track2_mp3.exists()
    assert track1_mp3.stat().st_mtime > track1_mtime
    # L'album reste publié : ces endpoints ne touchent jamais `published`.
    assert c.get(f"/api/albums/{slug}", headers=GEST).json()["published"] is True


def test_manifest_404(client):
    c, _ = client
    assert c.get("/api/jobs/nope/manifest", headers=GEST).status_code == 404


def test_has_editor_source_reflects_master_presence(client):
    """L'éditeur de coupes ne peut rouvrir que les albums qui ont conservé
    leur master (outil lien) — pas les imports manuels sans source/."""
    c, projects = client
    payload = {"album": {"artist": "Src", "title": "Alb", "date": "2026-06-06"},
               "tracks": [{"n": 1, "title": "A", "start": 0.0, "end": 3.0, "locked": True}],
               "target": "audio_cd"}
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    assert c.get(f"/api/albums/{slug}", headers=GEST).json()["has_editor_source"] is False
    _seed_master(projects / slug)
    assert c.get(f"/api/albums/{slug}", headers=GEST).json()["has_editor_source"] is True


def test_vitrine_public(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    r = c.get("/api/catalogue")
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_tool_shell_public_but_backend_gated(client):
    """Depuis 4bb250b, /app sert le shell HTML à tous : le verrouillage est
    côté client (overlay selon le rôle). Les routes sensibles restent
    protégées serveur : /app/album/{slug} et /api/jobs exigent gestionnaire."""
    c, _ = client
    assert c.get("/app").status_code == 200                  # shell public
    assert c.get("/app", headers=USER).status_code == 200
    assert c.get("/app", headers=GEST).status_code == 200
    assert c.get("/app/album/x").status_code == 401          # non connecté
    assert c.get("/app/album/x", headers=USER).status_code == 403


def test_download_requires_user(client):
    c, projects = client
    payload = {"album": {"artist": "DL", "title": "Alb", "date": "2026-03-03"},
               "tracks": [{"n": 1, "title": "A", "start": 0.0, "end": 3.0, "locked": True}],
               "target": "data_disc"}
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    c.patch(f"/api/albums/{slug}/published", json={"published": True}, headers=GEST)
    _seed_master(projects / slug)
    c.post(f"/api/jobs/{slug}/render", params={"media": "audio"}, headers=GEST)
    # attendre le rendu
    with c.stream("GET", f"/api/jobs/{slug}/events", headers=GEST) as resp:
        for line in resp.iter_lines():
            if line.startswith("data:") and json.loads(line[5:].strip())["status"] == "complete":
                break
    assert c.get(f"/download/{slug}/mp3").status_code == 401           # anonyme
    assert c.get(f"/download/{slug}/mp3", headers=USER).status_code == 200
    assert c.get(f"/download/{slug}/mp3", headers=GEST).status_code == 200


def test_me_anonymous_vs_roles(client):
    c, _ = client
    anon = c.get("/api/me").json()
    assert anon["authenticated"] is False and anon["is_gestionnaire"] is False
    g = c.get("/api/me", headers=GEST).json()
    assert g["authenticated"] and g["is_gestionnaire"] and g["is_user"]
    u = c.get("/api/me", headers=USER).json()
    assert u["authenticated"] and u["is_user"] and not u["is_gestionnaire"]


def test_labels_update_gestionnaire(client):
    c, projects = client
    payload = {"album": {"artist": "Lab", "title": "Alb", "date": "2026-05-05",
                         "labels": ["concert"]},
               "tracks": [{"n": 1, "title": "A", "start": 0.0, "end": 3.0, "locked": True}],
               "target": "data_disc"}
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    # anonyme interdit
    assert c.put(f"/api/albums/{slug}/labels", json={"labels": ["x"]}).status_code == 401
    assert c.put(f"/api/albums/{slug}/labels", json={"labels": ["x"]},
                 headers=USER).status_code == 403
    # gestionnaire : "audio" (dérivé) est filtré
    r = c.put(f"/api/albums/{slug}/labels", headers=GEST,
              json={"labels": ["festival", "high quality", "audio"]})
    assert r.status_code == 200
    labels = r.json()["labels"]
    assert "festival" in labels and "high quality" in labels
    assert "audio" not in labels
    d = c.get(f"/api/albums/{slug}", headers=GEST).json()
    assert set(d["labels"]) == {"festival", "high quality"}


def test_cover_download_requires_user(client, tmp_path):
    c, projects = client
    payload = {"album": {"artist": "Cov", "title": "Alb", "date": "2026-06-06",
                         "cover": "artwork/c.png"},
               "tracks": [{"n": 1, "title": "A", "start": 0.0, "end": 3.0, "locked": True}],
               "target": "data_disc"}
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    c.patch(f"/api/albums/{slug}/published", json={"published": True}, headers=GEST)
    art = projects / slug / "artwork"; art.mkdir(parents=True, exist_ok=True)
    (art / "c.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert c.get(f"/download/{slug}/cover").status_code == 401
    assert c.get(f"/download/{slug}/cover", headers=USER).status_code == 200


def test_catalogue_detail_public(client, tmp_path):
    c, projects = client
    payload = {"album": {"artist": "Det", "title": "Alb", "date": "2026-07-07"},
               "tracks": [{"n": 1, "title": "A", "start": 0.0, "end": 3.0, "locked": True},
                          {"n": 2, "title": "B", "start": 3.0, "end": 6.0, "locked": True}],
               "target": "data_disc"}
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    c.patch(f"/api/albums/{slug}/published", json={"published": True}, headers=GEST)
    _seed_master(projects / slug)
    c.post(f"/api/jobs/{slug}/render", params={"media": "audio"}, headers=GEST)
    with c.stream("GET", f"/api/jobs/{slug}/events", headers=GEST) as resp:
        for line in resp.iter_lines():
            if line.startswith("data:") and json.loads(line[5:].strip())["status"] == "complete":
                break
    d = c.get(f"/api/catalogue/{slug}").json()          # public, pas d'auth
    assert d["album"]["artist"] == "Det"
    assert [t["title"] for t in d["tracks"]] == ["A", "B"]
    assert all(t["dl"] for t in d["tracks"])


def test_track_download_requires_user(client, tmp_path):
    c, projects = client
    payload = {"album": {"artist": "Trk", "title": "Alb", "date": "2026-08-08"},
               "tracks": [{"n": 1, "title": "A", "start": 0.0, "end": 3.0, "locked": True}],
               "target": "data_disc"}
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    c.patch(f"/api/albums/{slug}/published", json={"published": True}, headers=GEST)
    _seed_master(projects / slug)
    c.post(f"/api/jobs/{slug}/render", params={"media": "audio"}, headers=GEST)
    with c.stream("GET", f"/api/jobs/{slug}/events", headers=GEST) as resp:
        for line in resp.iter_lines():
            if line.startswith("data:") and json.loads(line[5:].strip())["status"] == "complete":
                break
    assert c.get(f"/download/{slug}/track/1").status_code == 401
    r = c.get(f"/download/{slug}/track/1", headers=USER)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    assert c.get(f"/download/{slug}/track/9", headers=USER).status_code == 404


def test_healthz(client):
    c, _ = client
    assert c.get("/healthz").json() == {"ok": True}
    assert c.head("/healthz").status_code == 200


def test_version(client):
    c, _ = client
    v = c.get("/api/version").json()
    expected = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    assert v["version"] == expected
    assert v["env"] in ("prod", "preprod")
    assert "commit" in v


def _make_album(c, artist, title, date, published=False):
    payload = {"album": {"artist": artist, "title": title, "date": date},
               "tracks": [{"n": 1, "title": "A", "start": 0.0, "end": 3.0, "locked": True}],
               "target": "data_disc"}
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    if published:
        c.patch(f"/api/albums/{slug}/published", json={"published": True}, headers=GEST)
    return slug


def test_bulk_published_permissions_and_idempotency(client):
    c, _ = client
    s1 = _make_album(c, "Bulk", "One", "2026-01-01")
    s2 = _make_album(c, "Bulk", "Two", "2026-01-02")
    # anonyme / user interdits
    assert c.patch("/api/albums/bulk-published",
                   json={"slugs": [s1], "published": True}).status_code == 401
    assert c.patch("/api/albums/bulk-published", headers=USER,
                   json={"slugs": [s1], "published": True}).status_code == 403
    # gestionnaire : publie les deux + un slug inconnu ignoré
    r = c.patch("/api/albums/bulk-published", headers=GEST,
                json={"slugs": [s1, s2, "nope"], "published": True})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2 and set(body["updated"]) == {s1, s2}
    assert body["missing"] == ["nope"]
    # état reflété
    assert c.get(f"/api/albums/{s1}", headers=GEST).json()["published"] is True
    # idempotent : re-publier ne change rien
    r2 = c.patch("/api/albums/bulk-published", headers=GEST,
                 json={"slugs": [s1, s2], "published": True})
    assert r2.json()["count"] == 0
    # dépublier en masse
    r3 = c.patch("/api/albums/bulk-published", headers=GEST,
                 json={"slugs": [s1, s2], "published": False})
    assert r3.json()["count"] == 2
    assert c.get(f"/api/albums/{s1}", headers=GEST).json()["published"] is False


def test_bulk_download_auth_and_zip(client):
    import io as _io, zipfile as _zip
    c, projects = client
    # Un album rendu (mp3 réel) + un slug sans média : le second doit être ignoré.
    s1 = _make_album(c, "Zip", "Real", "2026-02-02", published=True)
    s2 = _make_album(c, "Zip", "Empty", "2026-02-03", published=True)
    _seed_master(projects / s1)
    c.post(f"/api/jobs/{s1}/render", params={"media": "audio"}, headers=GEST)
    with c.stream("GET", f"/api/jobs/{s1}/events", headers=GEST) as resp:
        for line in resp.iter_lines():
            if line.startswith("data:") and json.loads(line[5:].strip())["status"] == "complete":
                break
    # anonyme interdit (GET : téléchargement natif du navigateur)
    assert c.get("/download/bulk", params={"slugs": [s1], "kind": "mp3"}).status_code == 401
    # user : zip regroupant uniquement l'album qui a du média
    r = c.get("/download/bulk", params={"slugs": [s1, s2], "kind": "mp3"}, headers=USER)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    with _zip.ZipFile(_io.BytesIO(r.content)) as z:
        names = z.namelist()
    assert len(names) == 1 and names[0].endswith("_mp3.zip")
    # sélection sans média téléchargeable -> 404
    assert c.get("/download/bulk", params={"slugs": [s2], "kind": "mp3"},
                 headers=USER).status_code == 404
    # kind invalide -> 400
    assert c.get("/download/bulk", params={"slugs": [s1], "kind": "flac"},
                 headers=USER).status_code == 400
