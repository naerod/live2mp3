"""Re-couper une piste existante avec ses voisines (patron 3 pistes + cadenas).

Vue « triptyque » : la modale ouvre sur jusqu'à 3 pistes contiguës autour de N
(la précédente si elle existe, N, la suivante si elle existe). Les frontières
partagées portent un cadenas qui les fait bouger ensemble — c'est le patron
qu'un album live impose (les chansons s'enchaînent).

Ces tests verrouillent :
- la préparation renvoie bien les 3 pistes et l'état des frontières ;
- l'application d'un « recut lié » (2 pistes suivent la même valeur) modifie
  BIEN les 2 timecodes et ré-encode BIEN les 2 MP3 ;
- l'application d'un recut « target seule » ne touche PAS les voisines
  (mtime préservés) — invariant historique de la version v1 ;
- multi-liens et bornes hors master sont refusés proprement ;
- les cas de bord (N=1, N=last) ne demandent qu'une voisine.
"""
from __future__ import annotations

import struct
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import addtrack, recut
from backend.manifest import Manifest

GEST = {"X-authentik-username": "g", "X-authentik-groups": "live2mp3-gestionnaire"}
USER = {"X-authentik-username": "u", "X-authentik-groups": "live2mp3-user"}


VIDEO = {
    "title": "Coldplay & Ed Sheeran - Fix You (Live at Shepherd's Bush Empire)",
    "channel": "Coldplay",
    "duration": 307.0,
    "webpage_url": "https://www.youtube.com/watch?v=n9aL0otZalc",
    "thumbnail": "",
    "chapters": [],
}


# --- Fixtures --------------------------------------------------------------
@pytest.fixture
def api(tmp_path, monkeypatch, synth_audio_only):
    """Client FastAPI + un projet mono-source déjà rendu.

    Les 4 pistes du projet synthétique reçoivent leur MP3 dans `build/audio/` —
    ce sont eux qui sont modifiés par le recut. Le stub yt-dlp couvre aussi
    le fallback external sans réseau.
    """
    from backend import catalogue, covers, db, main, manifest, social
    from backend.pipeline import download, render
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "live2mp3.db")
    monkeypatch.setattr(db, "AVATARS_DIR", data / "avatars")
    monkeypatch.setattr(social, "AVATARS_DIR", data / "avatars")
    for mod in (manifest, main, catalogue, social, covers, addtrack, recut):
        monkeypatch.setattr(mod, "PROJECTS_DIR", synth_audio_only.parent)
    db.init_db()
    render.run(synth_audio_only, video=False)
    monkeypatch.setattr(addtrack.jellyfin, "refresh_album", lambda slug, **kw: True)
    monkeypatch.setattr(recut.jellyfin, "refresh_album", lambda slug, **kw: True)
    monkeypatch.setattr(addtrack, "probe_url", lambda url: dict(VIDEO))

    def fake_download(url, source_dir, cookies=None, progress=None):
        source_dir.mkdir(parents=True, exist_ok=True)
        dest = source_dir / "master_audio.wav"
        dest.write_bytes((synth_audio_only / "source" / "master.wav").read_bytes())
        if progress:
            progress(100.0)
        return dest

    monkeypatch.setattr(download, "download_audio", fake_download)
    return TestClient(main.app), synth_audio_only


def _wait_prep(client, slug, n, token, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/albums/{slug}/tracks/{n}/recut/{token}",
                       headers=GEST)
        if r.status_code != 200:
            raise AssertionError(f"status {r.status_code}: {r.text}")
        job = r.json()
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.1)
    raise AssertionError("préparation recut jamais terminée")


def _prep(client, slug, n):
    r = client.post(f"/api/albums/{slug}/tracks/{n}/recut/prep",
                    headers=GEST, json={})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    job = _wait_prep(client, slug, n, token)
    assert job["state"] == "done", job.get("error")
    return token, job


# --- Préparation -----------------------------------------------------------
def test_prep_middle_returns_three_tracks(api):
    """N=2 dans un projet à 4 pistes → prep retourne 3 pistes (N-1, N, N+1)
    avec 2 frontières partagées, toutes deux initialement liées (les projets
    synth ont start[k]==end[k-1] par construction)."""
    client, project = api
    _, job = _prep(client, project.name, 2)
    assert job["kind"] == "mono"
    tracks = job["tracks"]
    assert [t["n"] for t in tracks] == [1, 2, 3]
    # bornes ordonnées, chaque piste bien formée
    for t in tracks:
        assert t["end"] > t["start"]
    # les frontières sont contiguës (start[k+1] == end[k])
    assert tracks[1]["start"] == pytest.approx(tracks[0]["end"], abs=0.05)
    assert tracks[2]["start"] == pytest.approx(tracks[1]["end"], abs=0.05)
    bounds = job["boundaries"]
    assert [b["left_n"] for b in bounds] == [1, 2]
    assert [b["right_n"] for b in bounds] == [2, 3]
    assert all(b["linked"] for b in bounds)


def test_prep_first_track_has_no_prev(api):
    """N=1 : pas de piste précédente, la tranche commence à 0."""
    client, project = api
    _, job = _prep(client, project.name, 1)
    tracks = job["tracks"]
    assert [t["n"] for t in tracks] == [1, 2]
    assert job["ext_start"] == pytest.approx(0.0, abs=0.05)


def test_prep_last_track_has_no_next(api):
    """N=last : pas de piste suivante, la tranche s'arrête à master_duration."""
    client, project = api
    m = Manifest.load(project / "manifest.yaml")
    n = int(m.tracks[-1]["n"])
    _, job = _prep(client, project.name, n)
    tracks = job["tracks"]
    assert [t["n"] for t in tracks] == [n - 1, n]


def test_prep_serves_waveform_and_preview(api):
    """Waveform binaire audiowaveform + preview MP3 servis (mêmes patrons
    que prep-clip d'addtrack)."""
    client, project = api
    token, _ = _prep(client, project.name, 2)
    wf = client.get(f"/api/albums/{project.name}/tracks/2/recut/{token}/waveform.dat",
                    headers=GEST)
    assert wf.status_code == 200
    version = struct.unpack("<i", wf.content[:4])[0]
    # v1 ou v2 selon le binaire audiowaveform ; les deux sont supportés par
    # le composant (ClipTrimmer / MultiTrimmer).
    assert version in (1, 2)
    au = client.get(f"/api/albums/{project.name}/tracks/2/recut/{token}/audio",
                    headers=GEST)
    assert au.status_code == 200
    assert au.headers["content-type"] == "audio/mpeg"


def test_prep_master_absent_returns_error(api):
    """Master purgé → job passe en `error` (409 remonté en état async)."""
    client, project = api
    (project / "source" / "master.wav").unlink()
    r = client.post(f"/api/albums/{project.name}/tracks/2/recut/prep",
                    headers=GEST, json={})
    token = r.json()["token"]
    job = _wait_prep(client, project.name, 2, token)
    assert job["state"] == "error"


def test_prep_multi_link_rejected_synchronously(api):
    """Album multi-liens : 409 dès la sonde (avant même le sondage), avec
    un message qui pointe vers l'éditeur complet."""
    client, project = api
    # Convertit le projet en multi-source (`source.clips` non vide).
    m = Manifest.load(project / "manifest.yaml")
    m.data["source"]["clips"] = [
        {"url": f"https://ex/clip-{t['n']}", "duration": t["end"] - t["start"]}
        for t in m.tracks]
    m.save()
    r = client.post(f"/api/albums/{project.name}/tracks/2/recut/prep",
                    headers=GEST, json={})
    assert r.status_code == 409
    assert "multi" in r.json()["detail"].lower()


# --- Application : recut lié (cadenas fermé) -------------------------------
def test_apply_linked_boundary_edits_two_tracks(api):
    """Bouger la frontière N-N+1 (cadenas fermé) → 2 pistes modifiées
    (fin de N ET début de N+1 aux mêmes bornes), 2 MP3 ré-encodés."""
    client, project = api
    audio_dir = project / "build" / "audio"
    m0 = Manifest.load(project / "manifest.yaml")
    mp3_2 = audio_dir / m0.track_filename(m0.tracks[1], "mp3")
    mp3_3 = audio_dir / m0.track_filename(m0.tracks[2], "mp3")
    mp3_1 = audio_dir / m0.track_filename(m0.tracks[0], "mp3")
    mtime_1 = mp3_1.stat().st_mtime
    time.sleep(1.05)  # séparer les mtime pour la comparaison

    token, job = _prep(client, project.name, 2)
    # La frontière 2↔3 est initialement à 6 s (dans le master, donc
    # ext_start + 6 dans la tranche). On la déplace à 4 s (côté master) →
    # dans la tranche : 4 - ext_start.
    ext = float(job["ext_start"])
    new_boundary = 4.0 - ext
    p2, p3 = job["tracks"][1], job["tracks"][2]
    r2 = client.post(f"/api/albums/{project.name}/tracks/2/recut", headers=GEST,
                     json={"token": token, "edits": [
                         {"n": 2, "start": p2["start"], "end": new_boundary},
                         {"n": 3, "start": new_boundary, "end": p3["end"]},
                     ]})
    assert r2.status_code == 200, r2.text
    edited_ns = [e["n"] for e in r2.json()["edited"]]
    assert set(edited_ns) == {2, 3}

    m1 = Manifest.load(project / "manifest.yaml")
    # Timecodes bougés en manifest, ordonnés (start<end pour chaque piste,
    # frontière commune préservée).
    assert m1.tracks[1]["end"] == pytest.approx(4.0, abs=0.05)
    assert m1.tracks[2]["start"] == pytest.approx(4.0, abs=0.05)
    assert m1.tracks[2]["end"] == pytest.approx(9.0, abs=0.05)  # inchangé
    # Piste 1 (hors édit) : intacte en manifest ET fichier non touché.
    assert m1.tracks[0]["start"] == 0.0 and m1.tracks[0]["end"] == 3.0
    assert mp3_1.stat().st_mtime == mtime_1, "MP3 voisin ré-encodé à tort"
    # MP3 de N=2 raccourci (1s au lieu de 3s) et N=3 rallongé (5s au lieu de 3s).
    from mutagen.mp3 import MP3
    assert 0.8 < MP3(str(mp3_2)).info.length < 1.4
    assert 4.5 < MP3(str(mp3_3)).info.length < 5.5


def test_apply_target_only_leaves_neighbours_untouched(api):
    """Edit sur la seule piste ciblée (cadenas ouverts) → aucun MP3 voisin
    n'est ré-encodé (mtime préservés) et le manifest voisin est intact."""
    client, project = api
    audio_dir = project / "build" / "audio"
    m0 = Manifest.load(project / "manifest.yaml")
    mp3_1 = audio_dir / m0.track_filename(m0.tracks[0], "mp3")
    mp3_3 = audio_dir / m0.track_filename(m0.tracks[2], "mp3")
    mtime_1, mtime_3 = mp3_1.stat().st_mtime, mp3_3.stat().st_mtime
    time.sleep(1.05)

    token, job = _prep(client, project.name, 2)
    ext = float(job["ext_start"])
    r2 = client.post(f"/api/albums/{project.name}/tracks/2/recut", headers=GEST,
                     json={"token": token, "edits": [
                         {"n": 2, "start": 3.5 - ext, "end": 5.5 - ext},
                     ]})
    assert r2.status_code == 200, r2.text
    assert [e["n"] for e in r2.json()["edited"]] == [2]
    m1 = Manifest.load(project / "manifest.yaml")
    assert m1.tracks[1]["start"] == pytest.approx(3.5, abs=0.05)
    assert m1.tracks[1]["end"] == pytest.approx(5.5, abs=0.05)
    # Voisines intactes en manifest.
    assert m1.tracks[0]["end"] == 3.0
    assert m1.tracks[2]["start"] == 6.0
    # ET fichiers voisins non touchés.
    assert mp3_1.stat().st_mtime == mtime_1
    assert mp3_3.stat().st_mtime == mtime_3


def test_apply_rejects_edit_outside_prepared_slice(api):
    """Un edit sur une piste hors triptyque préparé est refusé (400),
    aucun effet de bord sur le manifest."""
    client, project = api
    before = Manifest.load(project / "manifest.yaml").data
    token, job = _prep(client, project.name, 2)
    r = client.post(f"/api/albums/{project.name}/tracks/2/recut", headers=GEST,
                    json={"token": token, "edits": [
                        {"n": 2, "start": 0.5, "end": 2.0},
                        {"n": 4, "start": 0.5, "end": 2.0},  # hors triptyque
                    ]})
    assert r.status_code == 400
    assert Manifest.load(project / "manifest.yaml").data == before


def test_apply_rejects_reversed_bounds(api):
    """Fin avant début sur n'importe quelle piste éditée → 400."""
    client, project = api
    before = Manifest.load(project / "manifest.yaml").data
    token, _ = _prep(client, project.name, 2)
    r = client.post(f"/api/albums/{project.name}/tracks/2/recut", headers=GEST,
                    json={"token": token, "edits": [
                        {"n": 2, "start": 5.0, "end": 3.5},
                    ]})
    assert r.status_code == 400
    assert Manifest.load(project / "manifest.yaml").data == before


def test_apply_requires_target_in_edits(api):
    """La piste ciblée par la modale DOIT figurer dans les edits, sinon 400
    (interface où on toucherait seulement les voisines n'a pas de sens)."""
    client, project = api
    token, _ = _prep(client, project.name, 2)
    r = client.post(f"/api/albums/{project.name}/tracks/2/recut", headers=GEST,
                    json={"token": token, "edits": [
                        {"n": 1, "start": 0.0, "end": 2.5},
                    ]})
    assert r.status_code == 400


def test_apply_rejects_stale_token(api):
    """Un token inconnu / périmé déclenche 409."""
    client, project = api
    r = client.post(f"/api/albums/{project.name}/tracks/2/recut", headers=GEST,
                    json={"token": "deadbeef", "edits": [
                        {"n": 2, "start": 0.5, "end": 2.5}]})
    assert r.status_code == 409


# --- Fallback external ------------------------------------------------------
def _add_external_track(project: Path, n: int, title: str) -> Path:
    """Ajoute au manifeste une piste externe déjà rendue + son MP3."""
    m = Manifest.load(project / "manifest.yaml")
    m.data["tracks"].append({
        "n": n, "title": title, "start": None, "end": None, "locked": False,
        "source": {"url": VIDEO["webpage_url"], "start": 1.0, "end": 4.0},
    })
    m.save()
    audio = project / "build" / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    mp3 = audio / m.track_filename(m.tracks[-1], "mp3")
    mp3.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00fake")
    return mp3


def test_prep_external_reuses_download_stub(api):
    """Fallback external : la source est re-téléchargée (yt-dlp stub) et
    waveform + preview produites, avec cur_start/cur_end de la piste."""
    client, project = api
    _add_external_track(project, 5, "Ext Song")
    r = client.post(f"/api/albums/{project.name}/tracks/5/recut/prep",
                    headers=GEST, json={})
    assert r.status_code == 200
    assert r.json()["kind"] == "external"
    token = r.json()["token"]
    job = _wait_prep(client, project.name, 5, token, timeout=15.0)
    assert job["state"] == "done", job.get("error")
    assert job["duration"] > 0
    assert job["cur_start"] == pytest.approx(1.0, abs=0.1)
    assert job["cur_end"] == pytest.approx(4.0, abs=0.1)


def test_apply_external_uses_start_end_and_updates_source(api):
    """External : signature {start,end} conservée (compat), source.start/end
    mis à jour dans le manifest."""
    client, project = api
    _add_external_track(project, 5, "Ext Song")
    r = client.post(f"/api/albums/{project.name}/tracks/5/recut/prep",
                    headers=GEST, json={})
    token = r.json()["token"]
    _wait_prep(client, project.name, 5, token, timeout=15.0)
    r2 = client.post(f"/api/albums/{project.name}/tracks/5/recut", headers=GEST,
                     json={"token": token, "start": 0.5, "end": 2.5})
    assert r2.status_code == 200, r2.text
    m1 = Manifest.load(project / "manifest.yaml")
    ext_track = next(t for t in m1.tracks if t["n"] == 5)
    assert ext_track["source"]["start"] == 0.5
    assert ext_track["source"]["end"] == 2.5


# --- Auth ------------------------------------------------------------------
def test_prep_requires_gestionnaire(api):
    client, project = api
    r = client.post(f"/api/albums/{project.name}/tracks/2/recut/prep", json={})
    assert r.status_code in (401, 403)
    r2 = client.post(f"/api/albums/{project.name}/tracks/2/recut/prep",
                     headers=USER, json={})
    assert r2.status_code in (401, 403)


def test_apply_requires_gestionnaire(api):
    client, project = api
    r = client.post(f"/api/albums/{project.name}/tracks/2/recut",
                    json={"token": "x", "edits": []})
    assert r.status_code in (401, 403)
