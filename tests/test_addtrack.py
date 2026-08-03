"""Ajout d'une piste isolée à un album existant, depuis un lien vidéo.

Trois choses peuvent silencieusement détruire du travail ici, et ce sont
elles que ces tests verrouillent :
- une piste externe n'a pas de timecodes d'album : le rendu ne doit ni tenter
  de la re-découper depuis le master, ni prendre son MP3 pour un orphelin ;
- l'éditeur de coupes réécrit la setlist **entière** : il doit recoller les
  pistes externes qu'il ne connaît pas ;
- un ajout qui échoue en cours de route ne doit pas laisser de piste fantôme
  dans le manifeste.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import addtrack
from backend.manifest import Manifest
from backend.pipeline import render

GEST = {"X-authentik-username": "g", "X-authentik-groups": "live2mp3-gestionnaire"}

VIDEO = {
    "title": "Coldplay & Ed Sheeran - Fix You (Live at Shepherd's Bush Empire)",
    "channel": "Coldplay",
    "duration": 307.0,
    "webpage_url": "https://www.youtube.com/watch?v=n9aL0otZalc",
    "thumbnail": "https://i.ytimg.com/vi/n9aL0otZalc/hq.jpg",
    "chapters": [],
}


# --- Découpage artiste / titre ---------------------------------------------

@pytest.mark.parametrize("title, channel, expected", [
    (VIDEO["title"], "Coldplay",
     ("Coldplay & Ed Sheeran", "Fix You (Live at Shepherd's Bush Empire)")),
    ("Shakira, Ed Sheeran, Beéle - Hips Don't Lie (Anniversary Version)", "Shakira",
     ("Shakira, Ed Sheeran, Beéle", "Hips Don't Lie (Anniversary Version)")),
    # Pas de séparateur : l'artiste vient de la chaîne.
    ("Bad Habits live at Wembley", "Ed Sheeran",
     ("Ed Sheeran", "Bad Habits live at Wembley")),
])
def test_split_artist_title(title, channel, expected):
    assert addtrack.split_artist_title(title, channel) == expected


def test_split_strips_noise_but_keeps_live_and_featuring():
    """« (Official Video) » est du bruit ; « (Live at …) » et « (ft. …) » sont
    précisément l'information qu'on veut garder sur une compilation live."""
    artist, title = addtrack.split_artist_title(
        "Sum 41 (ft. Mike Shinoda) - Faint (Official Music Video) [HD]", "Sum 41")
    assert artist == "Sum 41 (ft. Mike Shinoda)"
    assert title == "Faint"


def test_suggest_track_title_carries_the_artist():
    assert addtrack.suggest_track_title("Coldplay", "Fix You") == "Coldplay - Fix You"
    assert addtrack.suggest_track_title("", "Fix You") == "Fix You"


# --- Le rendu ignore les pistes externes -----------------------------------

def _add_external(project_dir: Path, title: str) -> Path:
    """Ajoute au manifeste une piste externe et son MP3 déjà produit."""
    m = Manifest.load(project_dir / "manifest.yaml")
    n = max(int(t["n"]) for t in m.tracks) + 1
    m.data["tracks"].append({
        "n": n, "title": title, "start": None, "end": None, "locked": False,
        "source": {"url": "https://www.youtube.com/watch?v=n9aL0otZalc"},
    })
    m.save()
    audio = project_dir / "build" / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    mp3 = audio / m.track_filename(m.tracks[-1], "mp3")
    mp3.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00fake")
    return mp3


def test_render_keeps_external_track_file(synth_audio_only):
    """Régression centrale : `_purge_orphans` ne garde que les fichiers des
    pistes ayant des timecodes. Une piste externe n'en a pas — sans exception
    explicite, chaque re-rendu de l'album effacerait son MP3."""
    mp3 = _add_external(synth_audio_only, "Coldplay (ft. Ed Sheeran) - Fix You")
    before = mp3.read_bytes()

    render.run(synth_audio_only, video=False, force=True)

    assert mp3.exists(), "le MP3 de la piste externe a été purgé"
    assert mp3.read_bytes() == before, "la piste externe a été ré-encodée"


def test_render_does_not_cut_external_track(synth_audio_only):
    """La piste externe n'est pas dans le lot à découper : elle n'a pas de
    timecodes, ffmpeg échouerait (ou produirait n'importe quoi)."""
    _add_external(synth_audio_only, "Coldplay (ft. Ed Sheeran) - Fix You")

    out = render.run(synth_audio_only, video=False)

    assert len(out["audio"]) == 4, "la piste externe a été envoyée au découpage"


# --- Cohabitation avec l'éditeur de coupes ---------------------------------

def test_setlist_edit_preserves_external_tracks(synth_audio_only, monkeypatch,
                                                tmp_path):
    from backend import catalogue, covers, db, main, manifest, social
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "live2mp3.db")
    monkeypatch.setattr(db, "AVATARS_DIR", data / "avatars")
    for mod in (manifest, main, catalogue, social, covers, addtrack):
        monkeypatch.setattr(mod, "PROJECTS_DIR", synth_audio_only.parent)
    db.init_db()
    client = TestClient(main.app)
    slug = synth_audio_only.name
    _add_external(synth_audio_only, "Coldplay (ft. Ed Sheeran) - Fix You")

    r = client.put(f"/api/jobs/{slug}/setlist", headers=GEST, json={"tracks": [
        {"n": 1, "title": "A", "start": 0.0, "end": 5.0},
        {"n": 2, "title": "B", "start": 5.0, "end": 12.0},
    ]})

    assert r.status_code == 200
    tracks = Manifest.load(synth_audio_only / "manifest.yaml").tracks
    assert [t["title"] for t in tracks] == [
        "A", "B", "Coldplay (ft. Ed Sheeran) - Fix You"]
    ext = tracks[-1]
    assert ext["n"] == 3, "la piste externe doit être renumérotée à la suite"
    assert ext["source"]["url"].endswith("n9aL0otZalc")
    assert ext["start"] is None and ext["end"] is None


# --- Parcours complet ------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch, synth_audio_only):
    from backend import catalogue, covers, db, main, manifest, social
    from backend.pipeline import download
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "live2mp3.db")
    monkeypatch.setattr(db, "AVATARS_DIR", data / "avatars")
    monkeypatch.setattr(social, "AVATARS_DIR", data / "avatars")
    for mod in (manifest, main, catalogue, social, covers, addtrack):
        monkeypatch.setattr(mod, "PROJECTS_DIR", synth_audio_only.parent)
    db.init_db()

    # Aucun accès réseau : la sonde et le téléchargement sont simulés, le
    # master synthétique du projet sert de source audio.
    monkeypatch.setattr(addtrack, "probe_url", lambda url: dict(VIDEO))

    def fake_download(url, source_dir, cookies=None, progress=None):
        source_dir.mkdir(parents=True, exist_ok=True)
        dest = source_dir / "master_audio.wav"
        dest.write_bytes((synth_audio_only / "source" / "master.wav").read_bytes())
        if progress:
            progress(100.0)
        return dest

    monkeypatch.setattr(download, "download_audio", fake_download)
    monkeypatch.setattr(addtrack.jellyfin, "refresh_album", lambda slug: True)
    return TestClient(main.app), synth_audio_only


def test_probe_track_prefills_the_form(api):
    client, _ = api
    r = client.post("/api/tool/probe-track", headers=GEST,
                    json={"url": VIDEO["webpage_url"]})
    assert r.status_code == 200
    sug = r.json()["suggestion"]
    assert sug["artist"] == "Coldplay & Ed Sheeran"
    assert sug["track_title"].startswith("Coldplay & Ed Sheeran - Fix You")


def _wait(client, slug, token, timeout=60.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/albums/{slug}/tracks/from-url/{token}",
                         headers=GEST).json()
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.2)
    raise AssertionError("ajout jamais terminé")


def test_add_track_produces_mp3_and_manifest_entry(api):
    client, project = api
    slug = project.name
    before = len(Manifest.load(project / "manifest.yaml").tracks)

    r = client.post(f"/api/albums/{slug}/tracks/from-url", headers=GEST, json={
        "url": VIDEO["webpage_url"],
        "title": "Coldplay (ft. Ed Sheeran) - Fix You",
        "start": 1.0, "end": 4.0,
        "cover_from_thumbnail": False,
    })
    assert r.status_code == 200
    job = _wait(client, slug, r.json()["token"])
    assert job["state"] == "done", job.get("error")

    tracks = Manifest.load(project / "manifest.yaml").tracks
    assert len(tracks) == before + 1
    added = tracks[-1]
    assert added["title"] == "Coldplay (ft. Ed Sheeran) - Fix You"
    assert added["start"] is None and added["end"] is None
    assert added["source"]["url"] == VIDEO["webpage_url"]
    assert added["source"]["start"] == 1.0 and added["source"]["end"] == 4.0
    assert added["source"]["added_by"] == "g"

    mp3 = project / "build" / "audio" / job["file"]
    assert mp3.exists() and mp3.stat().st_size > 0

    from mutagen.mp3 import MP3
    audio = MP3(str(mp3))
    assert 2.5 < audio.info.length < 3.5, "le rognage n'a pas été appliqué"


def test_failed_add_leaves_no_ghost_track(api, monkeypatch):
    """Un téléchargement en échec ne doit pas laisser dans le manifeste une
    piste sans fichier : elle apparaîtrait dans la vitrine et décalerait la
    numérotation ID3 de toutes les suivantes."""
    client, project = api
    from backend.pipeline import download

    def boom(*a, **kw):
        raise RuntimeError("yt-dlp a échoué : vidéo indisponible")

    monkeypatch.setattr(download, "download_audio", boom)
    slug = project.name
    before = [t["title"] for t in Manifest.load(project / "manifest.yaml").tracks]

    r = client.post(f"/api/albums/{slug}/tracks/from-url", headers=GEST, json={
        "url": VIDEO["webpage_url"], "title": "Piste morte",
        "cover_from_thumbnail": False})
    job = _wait(client, slug, r.json()["token"])

    assert job["state"] == "error"
    assert "yt-dlp" in job["error"]
    after = [t["title"] for t in Manifest.load(project / "manifest.yaml").tracks]
    assert after == before


def test_add_track_rejects_bad_trim(api):
    client, project = api
    r = client.post(f"/api/albums/{project.name}/tracks/from-url", headers=GEST,
                    json={"url": VIDEO["webpage_url"], "title": "X",
                          "start": 30.0, "end": 10.0})
    assert r.status_code == 400


def test_add_track_requires_gestionnaire(api):
    client, project = api
    r = client.post(f"/api/albums/{project.name}/tracks/from-url",
                    headers={"X-authentik-username": "u",
                             "X-authentik-groups": "live2mp3-user"},
                    json={"url": VIDEO["webpage_url"], "title": "X"})
    assert r.status_code in (401, 403)
