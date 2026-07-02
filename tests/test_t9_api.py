"""T9 — API : création job -> pipeline sur fixture -> états SSE."""
import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend import main, manifest
    monkeypatch.setattr(manifest, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(main, "PROJECTS_DIR", tmp_path)
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
    r = c.post("/api/jobs", json=payload)
    assert r.status_code == 200
    slug = r.json()["slug"]
    assert slug == "test-artist-2026-01-01"

    _seed_master(projects / slug)

    # manifest lisible via API
    m = c.get(f"/api/jobs/{slug}/manifest").json()
    assert len(m["tracks"]) == 4

    # lancement render + collecte SSE
    r = c.post(f"/api/jobs/{slug}/render", params={"media": "audio"})
    assert r.status_code == 200

    with c.stream("GET", f"/api/jobs/{slug}/events") as resp:
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
    slug = c.post("/api/jobs", json=payload).json()["slug"]
    r = c.put(f"/api/jobs/{slug}/markers", json={
        "tracks": {"1": {"start": 0.0, "end": 10.0},
                   "2": {"start": 10.0, "end": 20.0}}, "lock": True})
    assert r.status_code == 200
    m = c.get(f"/api/jobs/{slug}/manifest").json()
    assert m["tracks"][0]["locked"] is True
    assert m["tracks"][0]["start"] == 0.0


def test_manifest_404(client):
    c, _ = client
    assert c.get("/api/jobs/nope/manifest").status_code == 404
