"""Re-couper une piste existante à la waveform.

Trois cas cohabitent, chacun avec ses garde-fous :
- **mono-source** : le master est lu directement ; on ne touche pas aux
  pistes voisines et le ré-encodage porte uniquement sur la piste re-coupée.
- **multi-source** : la sous-tranche remplace la K-ième tranche du master
  ré-assemblé ; les timecodes cumulés doivent rester monotones (et
  contigus), le MP3 des pistes suivantes ne doit PAS être ré-encodé.
- **externe** : piste ajoutée depuis un lien (`track.source.url`) ; la
  source est re-préparée et le MP3 remplacé, mais rien du manifest album
  n'est touché (pas de timecodes d'album à recaler).

Un rollback (échec d'application) doit rendre l'album au même état :
manifest inchangé et MP3 précédent conservé.
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

    Toutes les pistes du projet synthétique reçoivent leur MP3 dans
    `build/audio/` — ce sont eux qui sont modifiés par le recut. Le stub
    de yt-dlp permet aussi de tester le fallback external sans réseau.
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

    # Rend l'album une fois : c'est l'état de départ (« album déjà produit »).
    render.run(synth_audio_only, video=False)
    monkeypatch.setattr(addtrack.jellyfin, "refresh_album", lambda slug: True)
    monkeypatch.setattr(recut.jellyfin, "refresh_album", lambda slug: True)

    # Sonde et téléchargement simulés pour les tests fallback external.
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


def _wav_seconds(path: Path) -> float:
    import wave
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


# --- Préparation ----------------------------------------------------------
def test_prep_master_present_serves_waveform_and_preview(api):
    """Chemin nominal : master.wav présent → lecture directe, aucun réseau.

    La tranche extraite doit contenir la piste + son contexte (poignées
    positionnées à cur_start/cur_end DANS la tranche, pas au bord).
    """
    client, project = api
    slug = project.name
    # Piste 2 : [3.0, 6.0] dans un master de 12 s (cf. conftest TRACK_SPANS).
    r = client.post(f"/api/albums/{slug}/tracks/2/recut/prep",
                    headers=GEST, json={})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["kind"] == "mono"
    token = d["token"]

    job = _wait_prep(client, slug, 2, token)
    assert job["state"] == "done", job.get("error")
    # 15 s de contexte avant/après, mais bornées par le master de 12 s :
    # tranche = [0, 12], donc les poignées ouvrent sur [3, 6].
    assert job["duration"] == pytest.approx(12.0, abs=0.15)
    assert job["cur_start"] == pytest.approx(3.0, abs=0.05)
    assert job["cur_end"] == pytest.approx(6.0, abs=0.05)
    assert job["ext_start"] == pytest.approx(0.0, abs=0.01)

    wf = client.get(
        f"/api/albums/{slug}/tracks/2/recut/{token}/waveform.dat",
        headers=GEST)
    assert wf.status_code == 200
    assert struct.unpack("<i", wf.content[:4])[0] == 2, "en-tête waveform.dat"

    au = client.get(f"/api/albums/{slug}/tracks/2/recut/{token}/audio",
                    headers=GEST)
    assert au.status_code == 200
    assert au.headers["content-type"] == "audio/mpeg"


def test_prep_master_absent_returns_409(api):
    """Master purgé (`L2M_PURGE_MASTERS=1` puis démonté) → 409 explicite,
    pas un piège silencieux qui laisserait l'utilisateur devant un token
    en erreur asynchrone."""
    client, project = api
    (project / "source" / "master.wav").unlink()
    r = client.post(f"/api/albums/{project.name}/tracks/2/recut/prep",
                    headers=GEST, json={})
    assert r.status_code == 200
    token = r.json()["token"]
    job = _wait_prep(client, project.name, 2, token)
    assert job["state"] == "error"


def test_prep_external_reuses_download_stub(api, monkeypatch):
    """Fallback external : la source est re-téléchargée (yt-dlp stub) et
    waveform + preview produites, comme addtrack.prep-clip."""
    client, project = api
    slug = project.name
    m = Manifest.load(project / "manifest.yaml")
    # Ajout d'une piste externe qui a survécu au rendu (fichier MP3 posé).
    m.data["tracks"].append({
        "n": 5, "title": "Ext Song", "start": None, "end": None,
        "locked": False,
        "source": {"url": VIDEO["webpage_url"], "start": 1.0, "end": 4.0},
    })
    m.save()
    (project / "build" / "audio" / m.track_filename(m.tracks[-1], "mp3")
     ).write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00fake")

    r = client.post(f"/api/albums/{slug}/tracks/5/recut/prep",
                    headers=GEST, json={})
    assert r.status_code == 200
    assert r.json()["kind"] == "external"
    token = r.json()["token"]
    job = _wait_prep(client, slug, 5, token, timeout=15.0)
    assert job["state"] == "done", job.get("error")
    assert job["duration"] > 0


# --- Application mono-source ----------------------------------------------
def test_apply_mono_updates_only_this_track(api):
    """Une re-coupe mono ne touche que la piste K : les voisines gardent
    leurs timecodes, leur MP3 n'est pas ré-encodé (mtime inchangé)."""
    client, project = api
    slug = project.name
    audio_dir = project / "build" / "audio"
    m0 = Manifest.load(project / "manifest.yaml")
    mp3_1 = audio_dir / m0.track_filename(m0.tracks[0], "mp3")
    mp3_3 = audio_dir / m0.track_filename(m0.tracks[2], "mp3")
    mtime_1 = mp3_1.stat().st_mtime
    mtime_3 = mp3_3.stat().st_mtime

    # Prépare puis re-coupe la piste 2 : la nouvelle sous-tranche = [4, 6] du
    # master (soit [4, 6] dans la tranche puisque ext_start=0).
    time.sleep(1.05)  # séparer les mtime pour comparer proprement ensuite
    r = client.post(f"/api/albums/{slug}/tracks/2/recut/prep",
                    headers=GEST, json={})
    token = r.json()["token"]
    _wait_prep(client, slug, 2, token)

    r2 = client.post(f"/api/albums/{slug}/tracks/2/recut", headers=GEST,
                     json={"token": token, "start": 4.0, "end": 6.0})
    assert r2.status_code == 200, r2.text
    d = r2.json()
    assert d["kind"] == "mono"
    assert d["start"] == pytest.approx(4.0, abs=0.05)
    assert d["end"] == pytest.approx(6.0, abs=0.05)

    m1 = Manifest.load(project / "manifest.yaml")
    assert m1.tracks[1]["start"] == pytest.approx(4.0, abs=0.05)
    assert m1.tracks[1]["end"] == pytest.approx(6.0, abs=0.05)
    # Voisines intactes en manifest.
    assert m1.tracks[0]["start"] == 0.0 and m1.tracks[0]["end"] == 3.0
    assert m1.tracks[2]["start"] == 6.0 and m1.tracks[2]["end"] == 9.0
    # ET fichiers voisins non touchés.
    assert mp3_1.stat().st_mtime == mtime_1, "MP3 voisin ré-encodé à tort"
    assert mp3_3.stat().st_mtime == mtime_3, "MP3 voisin ré-encodé à tort"
    # MP3 de la piste 2 : durée effective doit refléter la nouvelle coupe.
    mp3_2 = audio_dir / m1.track_filename(m1.tracks[1], "mp3")
    from mutagen.mp3 import MP3
    assert 1.5 < MP3(str(mp3_2)).info.length < 2.5, "coupe non appliquée"


# --- Application multi-source ---------------------------------------------
def _multi_project(base: Path) -> Path:
    """Convertit le projet mono en multi-source (`source.clips`).

    On duplique juste la déclaration : `clips` = même URL, une entrée par
    piste. Le master reste le même (déjà concaténé au rendu), et chaque
    piste est bien un segment contigu — les invariants du recut multi
    tiennent alors.
    """
    m = Manifest.load(base / "manifest.yaml")
    m.data.setdefault("source", {})["clips"] = [
        {"url": f"https://example.invalid/clip-{t['n']}",
         "duration": float(t["end"]) - float(t["start"])}
        for t in m.tracks]
    m.save()
    return base


def test_apply_multi_recomputes_cumulative_timecodes(api):
    """Multi-source : la piste K raccourcie de 1 s doit décaler les suivantes
    d'exactement 1 s (timecodes cumulés monotones)."""
    client, project = api
    slug = project.name
    _multi_project(project)
    audio_dir = project / "build" / "audio"

    m0 = Manifest.load(project / "manifest.yaml")
    old_starts = [t["start"] for t in m0.tracks]
    old_ends = [t["end"] for t in m0.tracks]
    mp3_3_bytes = (audio_dir / m0.track_filename(m0.tracks[2], "mp3")).read_bytes()

    # Piste 2 : tranche extraite = [0, 12] (ext_start=0 vu la taille du master).
    # On raccourcit la piste 2 de 1 s : nouvelle sous-tranche [3, 5] au lieu
    # de [3, 6]. Bornes envoyées = ext_start-relatives, donc [3, 5].
    r = client.post(f"/api/albums/{slug}/tracks/2/recut/prep",
                    headers=GEST, json={})
    token = r.json()["token"]
    _wait_prep(client, slug, 2, token)

    r2 = client.post(f"/api/albums/{slug}/tracks/2/recut", headers=GEST,
                     json={"token": token, "start": 3.0, "end": 5.0})
    assert r2.status_code == 200, r2.text
    assert r2.json()["kind"] == "multi"

    m1 = Manifest.load(project / "manifest.yaml")
    # Timecodes monotones et contigus (chaque start = end précédent).
    for i in range(1, len(m1.tracks)):
        assert m1.tracks[i]["start"] == pytest.approx(
            m1.tracks[i - 1]["end"], abs=0.05), \
            f"discontinuité entre pistes {i} et {i+1}"
    # Piste 2 = 2 s, les suivantes suivent avec le décalage de -1 s.
    assert m1.tracks[0]["start"] == pytest.approx(old_starts[0], abs=0.05)
    assert m1.tracks[0]["end"] == pytest.approx(old_ends[0], abs=0.05)
    assert (m1.tracks[1]["end"] - m1.tracks[1]["start"]) == pytest.approx(
        2.0, abs=0.05)
    # Piste 3 : décalée de -1 s en début ET en fin (ses bytes n'ont pas changé).
    assert m1.tracks[2]["start"] == pytest.approx(old_starts[2] - 1.0, abs=0.05)
    assert (m1.tracks[2]["end"] - m1.tracks[2]["start"]) == pytest.approx(
        old_ends[2] - old_starts[2], abs=0.05)


def test_apply_multi_master_shrinks_by_delta(api):
    """Le master ré-assemblé doit avoir perdu exactement le delta de la
    coupe (invariant de conservation d'énergie du multi-recut)."""
    client, project = api
    slug = project.name
    _multi_project(project)
    master = project / "source" / "master.wav"
    old_dur = _wav_seconds(master)

    r = client.post(f"/api/albums/{slug}/tracks/2/recut/prep",
                    headers=GEST, json={})
    token = r.json()["token"]
    _wait_prep(client, slug, 2, token)
    r2 = client.post(f"/api/albums/{slug}/tracks/2/recut", headers=GEST,
                     json={"token": token, "start": 3.0, "end": 5.0})
    assert r2.status_code == 200
    new_dur = _wav_seconds(master)
    assert new_dur == pytest.approx(old_dur - 1.0, abs=0.1)


# --- Rollback / robustesse -------------------------------------------------
def test_apply_rejects_reversed_bounds(api):
    """Fin avant début → 400, aucun effet de bord sur le manifest."""
    client, project = api
    slug = project.name
    before = Manifest.load(project / "manifest.yaml").data
    r = client.post(f"/api/albums/{slug}/tracks/2/recut/prep",
                    headers=GEST, json={})
    token = r.json()["token"]
    _wait_prep(client, slug, 2, token)

    r2 = client.post(f"/api/albums/{slug}/tracks/2/recut", headers=GEST,
                     json={"token": token, "start": 6.0, "end": 5.0})
    assert r2.status_code == 400
    after = Manifest.load(project / "manifest.yaml").data
    assert after == before


def test_apply_rejects_stale_token(api):
    """Un token dont la préparation a été purgée (ou n'a jamais existé)
    déclenche 409 : sinon on lirait la mauvaise tranche du master."""
    client, project = api
    r = client.post(f"/api/albums/{project.name}/tracks/2/recut", headers=GEST,
                    json={"token": "deadbeef", "start": 3.0, "end": 5.0})
    assert r.status_code == 409


# --- Auth ------------------------------------------------------------------
def test_prep_requires_gestionnaire(api):
    client, project = api
    r = client.post(f"/api/albums/{project.name}/tracks/2/recut/prep",
                    json={})
    assert r.status_code in (401, 403)
    r2 = client.post(f"/api/albums/{project.name}/tracks/2/recut/prep",
                     headers=USER, json={})
    assert r2.status_code in (401, 403)


def test_apply_requires_gestionnaire(api):
    client, project = api
    r = client.post(f"/api/albums/{project.name}/tracks/2/recut",
                    json={"token": "x", "start": 0, "end": 1})
    assert r.status_code in (401, 403)
