"""Fixtures de test — projet synthétique généré par ffmpeg.

Aucun téléchargement réseau : on fabrique un master audio (bips espacés)
et un master vidéo (mire + tonalité) courts, plus un manifest complet.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import fakeredis
import pytest

# 4 pistes de 3 s chacune -> master de 12 s. Timecodes connus.
TRACK_SPANS = [(0.0, 3.0), (3.0, 6.0), (6.0, 9.0), (9.0, 12.0)]


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """Redis en mémoire pour toute la suite.

    La progression et la file de rendu sont passées par Redis quand le rendu a
    quitté le process API pour le worker RQ. Sans ce doublon, la suite exige un
    serveur Redis joignable et échoue hors docker.

    Un seul `FakeServer` pour les trois connexions : `renderqueue` en ouvre deux
    (binaire pour RQ, décodée pour ses propres clés) et elles doivent voir le
    même espace de clés, sinon le garde-fou anti-doublon ne relit pas ce que la
    mise en file a écrit.

    La file est en mode synchrone (`is_async=False`) : aucun worker RQ ne tourne
    pendant les tests, donc un rendu mis en file ne serait jamais exécuté et le
    flux SSE attendrait indéfiniment un évènement final. En synchrone, le rendu
    s'exécute dans l'appel à `enqueue`, et le rejeu de l'historique par
    `/events` trouve la progression déjà complète.
    """
    server = fakeredis.FakeServer()
    from rq import Queue

    from backend import progress, renderqueue

    monkeypatch.setattr(progress, "_client",
                        fakeredis.FakeRedis(server=server, decode_responses=True))
    monkeypatch.setattr(renderqueue, "_rq_conn",
                        fakeredis.FakeRedis(server=server))
    monkeypatch.setattr(renderqueue, "_kv_conn",
                        fakeredis.FakeRedis(server=server, decode_responses=True))
    monkeypatch.setattr(renderqueue, "queue", lambda: Queue(
        renderqueue.QUEUE_NAME, connection=renderqueue.rq_conn(),
        default_timeout=renderqueue.JOB_TIMEOUT, is_async=False))


def _make_master_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 4 tonalités distinctes concaténées, 3 s chacune.
    freqs = [220, 330, 440, 550]
    parts = []
    filters = []
    for i, f in enumerate(freqs):
        filters.append(f"sine=frequency={f}:duration=3[a{i}]")
    concat_in = "".join(f"[a{i}]" for i in range(len(freqs)))
    fc = ";".join(filters) + f";{concat_in}concat=n={len(freqs)}:v=0:a=1[out]"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc",  # placeholder input unused
        "-filter_complex", fc, "-map", "[out]",
        "-ar", "44100", "-ac", "2", str(path),
    ], check=True, capture_output=True)


def _make_master_mkv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        "-c:a", "aac", "-shortest", str(path),
    ], check=True, capture_output=True)


def _make_cover(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "color=c=indigo:size=600x600:duration=1",
        "-frames:v", "1", str(path),
    ], check=True, capture_output=True)


@pytest.fixture
def synth_project(tmp_path) -> Path:
    """Crée un projet complet prêt pour render/tags, retourne project_dir."""
    from backend.manifest import new_manifest

    project_dir = tmp_path / "twenty-one-pilots-2026-04-03"
    project_dir.mkdir(parents=True)

    _make_master_wav(project_dir / "source" / "master.wav")
    _make_master_mkv(project_dir / "source" / "master.mkv")
    _make_cover(project_dir / "artwork" / "cover_front.png")

    titles = ["Overcompensate", "Vignette", "Shy Away / Heathens / Next Semester", "Trees"]
    tracks = []
    for i, (title, (start, end)) in enumerate(zip(titles, TRACK_SPANS), start=1):
        t = {"n": i, "title": title, "start": start, "end": end, "locked": True}
        if "/" in title:
            t["parts"] = [p.strip() for p in title.split("/")]
        tracks.append(t)

    album = {
        "artist": "Twenty One Pilots",
        "title": "The Clancy Tour · Breach — Live in Indianapolis",
        "date": "2026-04-03",
        "venue": "American Legion Mall, Indianapolis, IN",
        "festival": "AT&T Block Party",
        "cover": "artwork/cover_front.png",
    }
    m = new_manifest(album, tracks, target="data_disc",
                     source_url="https://example.invalid/stream")
    m.save(project_dir / "manifest.yaml")
    return project_dir


@pytest.fixture
def synth_audio_only(tmp_path) -> Path:
    """Projet audio-only (pas de master.mkv) pour tests rapides."""
    from backend.manifest import new_manifest

    project_dir = tmp_path / "test-artist-2026-01-01"
    project_dir.mkdir(parents=True)
    _make_master_wav(project_dir / "source" / "master.wav")

    tracks = [
        {"n": i, "title": t, "start": s, "end": e, "locked": True}
        for i, (t, (s, e)) in enumerate(
            zip(["A", "B", "C", "D"], TRACK_SPANS), start=1)
    ]
    album = {"artist": "Test Artist", "title": "Test Album", "date": "2026-01-01"}
    m = new_manifest(album, tracks, target="audio_cd")
    m.save(project_dir / "manifest.yaml")
    return project_dir
