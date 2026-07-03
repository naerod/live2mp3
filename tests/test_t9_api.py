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
    from backend import catalogue, main, manifest
    monkeypatch.setattr(manifest, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(main, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(catalogue, "PROJECTS_DIR", tmp_path)
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
    assert {"render", "tags", "artwork", "disc", "bundle"} <= stages_done


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


def test_manifest_404(client):
    c, _ = client
    assert c.get("/api/jobs/nope/manifest", headers=GEST).status_code == 404


def test_vitrine_public(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    r = c.get("/api/catalogue")
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_tool_requires_gestionnaire(client):
    c, _ = client
    assert c.get("/app").status_code == 401                 # non connecté
    assert c.get("/app", headers=USER).status_code == 403    # user pas gestionnaire
    assert c.get("/app", headers=GEST).status_code == 200


def test_download_requires_user(client):
    c, projects = client
    payload = {"album": {"artist": "DL", "title": "Alb", "date": "2026-03-03"},
               "tracks": [{"n": 1, "title": "A", "start": 0.0, "end": 3.0, "locked": True}],
               "target": "data_disc"}
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
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
    art = projects / slug / "artwork"; art.mkdir(parents=True, exist_ok=True)
    (art / "c.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert c.get(f"/download/{slug}/cover").status_code == 401
    assert c.get(f"/download/{slug}/cover", headers=USER).status_code == 200
