"""Vidéo d'un album existant (`backend/albumvideo.py`).

Cas réel à l'origine (2026-09-27) : « Coldplay — Live at Glastonbury 2024 »,
album audio, auquel on veut rattacher un export DaVinci (.mov H.264 + PCM
24 bits). Deux modes : garder les MP3 et rattacher la vidéo telle quelle, ou
redécouper les MP3 depuis cette nouvelle source.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.manifest import Manifest
from backend.pipeline import render

GEST = {"X-authentik-username": "g", "X-authentik-groups": "live2mp3-gestionnaire"}
USER = {"X-authentik-username": "u", "X-authentik-groups": "live2mp3-user"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from backend import (albumvideo, catalogue, covers, db, import_album, main,
                         manifest, social)
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "live2mp3.db")
    monkeypatch.setattr(db, "AVATARS_DIR", data / "avatars")
    monkeypatch.setattr(social, "AVATARS_DIR", data / "avatars")
    for mod in (manifest, main, catalogue, social, covers, import_album, albumvideo):
        monkeypatch.setattr(mod, "PROJECTS_DIR", tmp_path)
    db.init_db()
    return TestClient(main.app), tmp_path


def _mov(path: Path, seconds: int = 5) -> bytes:
    """Export façon DaVinci : QuickTime, H.264, audio PCM 24 bits 48 kHz."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=660:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        "-c:a", "pcm_s24le", "-ar", "48000", "-ac", "2", "-shortest",
        "-f", "mov", str(path),
    ], check=True, capture_output=True)
    return path.read_bytes()


def _upload(c, name: str, data: bytes) -> tuple[str, str]:
    init = c.post("/api/import/upload/init", headers=GEST,
                  json={"filename": name, "size": len(data)}).json()
    tok, fid = init["token"], init["file_id"]
    r = c.put(f"/api/import/upload/{tok}/{fid}",
              headers={**GEST, "X-Chunk-Offset": "0"}, content=data)
    assert r.status_code == 200, r.text
    fin = c.post(f"/api/import/upload/{tok}/{fid}/finish", headers=GEST,
                 json={"size": len(data)})
    assert fin.status_code == 200, fin.text
    return tok, fin.json()["file"]


def _render_audio(project: Path) -> dict[str, float]:
    render.run(project, video=False)
    return {p.name: p.stat().st_mtime for p in (project / "build" / "audio").glob("*.mp3")}


def test_keep_rattache_la_video_entiere_sans_toucher_aux_mp3(client, synth_audio_only):
    c, root = client
    slug = synth_audio_only.name
    mp3_before = _render_audio(synth_audio_only)
    assert mp3_before

    tok, name = _upload(c, "Timeline 1.mov", _mov(root / "in" / "t.mov", seconds=5))
    r = c.post(f"/api/albums/{slug}/video", headers=GEST,
               json={"token": tok, "file": name, "mode": "keep"})
    assert r.status_code == 200, r.text

    m = Manifest.load(synth_audio_only / "manifest.yaml")
    assert m.data["source"]["video_attached"] == "source/video_attached.mov"
    # File synchrone en test : le rendu vidéo a déjà tourné.
    mp4s = list((synth_audio_only / "build" / "video-full").glob("*.mp4"))
    assert len(mp4s) == 1
    # Rendue EN ENTIER (5 s), pas sur l'empan des pistes (12 s de master audio).
    assert render.media_seconds(mp4s[0]) == pytest.approx(5.0, abs=0.3)
    # Les MP3 n'ont pas bougé.
    after = {p.name: p.stat().st_mtime
             for p in (synth_audio_only / "build" / "audio").glob("*.mp3")}
    assert after == mp3_before
    # Staging consommé.
    assert not (root / ".l2m-import" / tok).exists()


def test_recut_remplace_le_master_et_efface_les_coupes(client, synth_audio_only):
    c, root = client
    slug = synth_audio_only.name
    titles = [t["title"] for t in Manifest.load(synth_audio_only / "manifest.yaml").tracks]

    tok, name = _upload(c, "concert.mov", _mov(root / "in" / "c.mov", seconds=6))
    r = c.post(f"/api/albums/{slug}/video", headers=GEST,
               json={"token": tok, "file": name, "mode": "recut"})
    assert r.status_code == 200, r.text

    m = Manifest.load(synth_audio_only / "manifest.yaml")
    assert [t["title"] for t in m.tracks] == titles          # titres conservés
    assert all("start" not in t and "end" not in t for t in m.tracks)
    assert m.data["source"]["media"] == "video"
    assert m.data["source"]["duration"] == pytest.approx(6.0, abs=0.3)
    assert m.data["rerender_pending"] is True
    assert m.state("ai_markers") == "pending" and m.state("download") == "done"
    assert (synth_audio_only / "source" / "master.mkv").is_file()


def test_garde_fous(client, synth_audio_only):
    c, root = client
    slug = synth_audio_only.name
    tok, name = _upload(c, "v.mov", _mov(root / "in" / "v.mov", seconds=2))
    body = {"token": tok, "file": name, "mode": "keep"}

    assert c.post(f"/api/albums/{slug}/video", headers=USER, json=body).status_code == 403
    assert c.post(f"/api/albums/{slug}/video", headers=GEST,
                  json={**body, "mode": "autre"}).status_code == 400
    assert c.post("/api/albums/inconnu/video", headers=GEST, json=body).status_code == 404

    # Album possédé par l'autre environnement (volume partagé) : refus.
    m = Manifest.load(synth_audio_only / "manifest.yaml")
    m.data["origin_env"] = "autre-env"
    m.save(touch=False)
    r = c.post(f"/api/albums/{slug}/video", headers=GEST, json=body)
    assert r.status_code == 409
    # Rien n'a été consommé : le fichier est toujours dans le staging.
    assert (root / ".l2m-import" / tok / "files" / name).is_file()


def test_rendu_force_apres_redecoupage_meme_depublie(client, synth_audio_only, monkeypatch):
    """Les noms de fichiers ne changent pas : sans rendu forcé, les anciens MP3
    (ancienne source) survivraient au redécoupage d'un album dépublié."""
    from backend import renderqueue
    c, _ = client
    m = Manifest.load(synth_audio_only / "manifest.yaml")
    m.data["published"] = False
    m.data["rerender_pending"] = True
    m.save(touch=False)
    seen = {}
    monkeypatch.setattr(renderqueue, "enqueue",
                        lambda slug, **kw: seen.update(kw))
    r = c.post(f"/api/jobs/{synth_audio_only.name}/render", headers=GEST)
    assert r.status_code == 200, r.text
    assert seen["republish"] is True
