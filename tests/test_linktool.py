"""Outil « album depuis un lien » : analyse, préparation, setlist, rendu.

Aucun réseau : yt-dlp et DeepSeek sont mockés, l'audio vient des fixtures
ffmpeg synthétiques.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import linktool, llm, progress
from backend.pipeline import boundaries
from backend.pipeline.tags import tag_mp3

GEST = {"X-authentik-username": "g", "X-authentik-groups": "live2mp3-gestionnaire"}
USER = {"X-authentik-username": "u", "X-authentik-groups": "live2mp3-user"}


@pytest.fixture
def client(tmp_path, monkeypatch):
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


# --- Sonde et heuristique --------------------------------------------------
RAW_INFO = {
    "id": "abc123", "title": "U2 - Live In Times Square (Full Show)",
    "channel": "SomeChannel", "upload_date": "20141203", "duration": 1560.0,
    "description": "Concert du 1er décembre 2014.",
    "chapters": [
        {"title": "01. Beautiful Day", "start_time": 0, "end_time": 300},
        {"title": "[05:00] With or Without You", "start_time": 300, "end_time": 640},
    ],
    "thumbnail": "https://img.example/maxres.jpg",
    "webpage_url": "https://youtube.example/watch?v=abc123",
    "extractor_key": "Youtube",
}


def test_normalize_info():
    v = linktool.normalize_info(RAW_INFO, "https://youtube.example/watch?v=abc123")
    assert v["upload_date"] == "2014-12-03"
    assert v["duration"] == 1560.0
    assert len(v["chapters"]) == 2
    assert v["chapters"][0] == {"title": "01. Beautiful Day", "start": 0.0,
                               "end": 300.0}


def test_clean_chapter_title():
    assert linktool.clean_chapter_title("01. Beautiful Day") == "Beautiful Day"
    assert linktool.clean_chapter_title("[05:00] With or Without You") == \
        "With or Without You"
    assert linktool.clean_chapter_title("3) Vertigo") == "Vertigo"
    assert linktool.clean_chapter_title("Angel of Harlem") == "Angel of Harlem"


def test_heuristic_suggestion_uses_chapters_and_title():
    v = linktool.normalize_info(RAW_INFO, "u")
    s = linktool.heuristic_suggestion(v)
    assert s["artist"] == "U2"          # partie avant « - » du titre
    assert len(s["tracks"]) == 2
    assert s["tracks"][0]["title"] == "Beautiful Day"
    assert s["tracks"][0]["start"] == 0.0
    assert s["tracks"][1]["title"] == "With or Without You"
    assert s["date"] == "2014-12-03"


def test_heuristic_suggestion_falls_back_to_channel():
    v = dict(linktool.normalize_info(RAW_INFO, "u"),
             title="Live In Times Square", channel="U2VEVO")
    s = linktool.heuristic_suggestion(v)
    assert s["artist"] == "U2"          # suffixe VEVO retiré


# --- Parsing LLM -----------------------------------------------------------
def test_parse_album_info_valid():
    resp = json.dumps({
        "artist": "U2", "title": "Live in Times Square 2014",
        "date": "2014-12-01", "venue": "Times Square", "city": "New York",
        "festival": "World AIDS Day", "tracks": [
            {"n": 1, "title": "Beautiful Day", "artist": "U2 with Chris Martin",
             "start": 0, "end": 300},
            {"n": 2, "title": "With or Without You", "artist": None,
             "start": 300, "end": 9999},
        ]})
    out = llm.parse_album_info(resp, duration=1560.0)
    assert out["date"] == "2014-12-01"
    assert out["tracks"][0]["artist"] == "U2 with Chris Martin"
    assert out["tracks"][1]["artist"] is None
    assert out["tracks"][1]["end"] == 1560.0   # borné à la durée


def test_parse_album_info_bad_date_and_renumber():
    resp = json.dumps({"artist": "X", "title": "Y", "date": "1 déc 2014",
                       "tracks": [{"n": 7, "title": "A"},
                                  {"n": 9, "title": ""},
                                  {"n": 12, "title": "B"}]})
    out = llm.parse_album_info(resp)
    assert out["date"] is None
    assert [t["n"] for t in out["tracks"]] == [1, 2]   # vide écartée, renuméroté
    assert out["tracks"][1]["title"] == "B"


def test_extract_album_info_calls_chat(monkeypatch):
    captured = {}
    def fake_chat(system, user, timeout=120):
        captured["user"] = user
        return json.dumps({"artist": "U2", "title": "T", "tracks": []})
    monkeypatch.setattr(llm, "_chat", fake_chat)
    v = linktool.normalize_info(RAW_INFO, "u")
    out = llm.extract_album_info(v)
    assert out["artist"] == "U2"
    assert "Beautiful Day" in captured["user"]   # chapitres transmis au LLM


def test_request_auto_setlist_parsing(monkeypatch):
    def fake_chat(system, user, timeout=180):
        return json.dumps({"tracks": [
            {"n": 1, "title": "Song A", "start": 0, "end": 200},
            {"n": 2, "title": "Song B", "artist": "Guest", "start": 200,
             "end": 5000},
            {"n": 3, "title": "", "start": 300, "end": 400},       # sans titre
            {"n": 4, "title": "Bad", "start": 500, "end": 400},    # end<start
        ]})
    monkeypatch.setattr(llm, "_chat", fake_chat)
    tracks = llm.request_auto_setlist([], [{"time": 200.0, "depth": 9.0}], "U2", 900.0)
    assert len(tracks) == 2
    assert tracks[1]["artist"] == "Guest"
    assert tracks[1]["end"] == 900.0


# --- Découpe de secours ----------------------------------------------------
def test_fallback_markers_uses_deepest_energy_dips():
    """Les coupes tombent sur les creux les plus marqués, pas les plus proches."""
    from backend.main import _fallback_markers
    cands = [{"time": 3.0, "depth": 12.0}, {"time": 6.0, "depth": 15.0},
             {"time": 9.0, "depth": 9.0}, {"time": 4.0, "depth": 1.2}]
    mk = _fallback_markers(4, 12.0, cands)
    assert list(mk) == [1, 2, 3, 4]
    assert mk[1]["start"] == 0.0 and mk[4]["end"] == 12.0
    # le creux à 4.0 (profondeur 1.2) est écarté au profit des trois marqués
    assert mk[1]["end"] == pytest.approx(3.0)
    assert mk[2]["end"] == pytest.approx(6.0)
    assert mk[3]["end"] == pytest.approx(9.0)


def test_fallback_markers_even_split_without_candidates():
    from backend.main import _fallback_markers
    mk = _fallback_markers(3, 9.0, [])
    assert mk[1] == {"start": 0.0, "end": 3.0}
    assert mk[3] == {"start": 6.0, "end": 9.0}


# --- Détection des frontières par l'énergie --------------------------------
def test_energy_candidates_find_dips():
    """Un creux d'énergie franc doit être détecté ; le bruit de fond non."""
    import numpy as np
    from backend.pipeline import boundaries as B
    step = B.STEP
    n = int(600 / step)                       # 10 min
    env = np.full(n, 8000.0, dtype=np.float32)
    rng = np.random.default_rng(0)
    env += rng.normal(0, 200, n).astype(np.float32)   # grain
    for t in (200.0, 400.0):                  # deux vraies transitions
        i = int(t / step)
        env[i - 6:i + 6] = 200.0
    cands = B.candidates_from_envelope(env, edge=30.0)
    times = [c["time"] for c in cands]
    assert any(abs(t - 200.0) < 3 for t in times), times
    assert any(abs(t - 400.0) < 3 for t in times), times
    # Les deux creux francs sont de loin les plus profonds
    assert B.pick(cands, 2) == pytest.approx([200.0, 400.0], abs=3.0)


def test_energy_candidates_ignore_close_dips():
    """Deux creux rapprochés ne peuvent pas être deux coupes (durée plancher)."""
    import numpy as np
    from backend.pipeline import boundaries as B
    step = B.STEP
    n = int(600 / step)
    env = np.full(n, 8000.0, dtype=np.float32)
    for t in (300.0, 320.0):                  # 20 s d'écart
        i = int(t / step)
        env[i - 6:i + 6] = 300.0
    cands = B.candidates_from_envelope(env, edge=30.0)
    kept = [c["time"] for c in cands if 280 < c["time"] < 340]
    assert len(kept) == 1, kept


def test_snap_markers_realigns_on_dip():
    """L'IA situe la transition à quelques secondes près : on recale."""
    from backend.main import _snap_markers
    cands = [{"time": 281.7, "depth": 13.7}, {"time": 602.2, "depth": 16.7}]
    markers = {1: {"start": 0.0, "end": 290.0},
               2: {"start": 290.0, "end": 610.0},
               3: {"start": 610.0, "end": 900.0}}
    out = _snap_markers(markers, cands)
    assert out[1]["end"] == pytest.approx(281.7)
    assert out[2]["start"] == pytest.approx(281.7)   # frontière commune
    assert out[2]["end"] == pytest.approx(602.2)
    assert out[1]["start"] == 0.0                    # bords intouchés
    assert out[3]["end"] == 900.0


def test_snap_markers_keeps_far_boundary():
    """Sans creux à portée (medley), la frontière n'est pas déplacée."""
    from backend.main import _snap_markers
    cands = [{"time": 100.0, "depth": 10.0}]
    markers = {1: {"start": 0.0, "end": 400.0}, 2: {"start": 400.0, "end": 800.0}}
    out = _snap_markers(markers, cands)
    assert out[1]["end"] == 400.0


# --- Routes API ------------------------------------------------------------
def _job_payload(**over):
    p = {"album": {"artist": "Test Artist", "title": "Live Album",
                   "date": "2026-01-01", "venue": "Somewhere"},
         "tracks": [{"n": 1, "title": "A", "start": 0.0, "end": 3.0}],
         "target": "data_disc", "source_url": "https://example.invalid/v",
         "duration": 12.0}
    p.update(over)
    return p


def test_create_job_conflict_409(client):
    c, _ = client
    assert c.post("/api/jobs", json=_job_payload(), headers=GEST).status_code == 200
    r = c.post("/api/jobs", json=_job_payload(), headers=GEST)
    assert r.status_code == 409


def test_create_job_auto_setlist_and_meta(client):
    c, projects = client
    r = c.post("/api/jobs", json=_job_payload(tracks=[]), headers=GEST)
    assert r.status_code == 200
    slug = r.json()["slug"]
    m = c.get(f"/api/jobs/{slug}/manifest", headers=GEST).json()
    assert m["auto_setlist"] is True
    assert len(m["tracks"]) == 1          # piste provisoire
    assert m["meta"]["imported_by"] == "g"
    assert m["meta"]["import_source"] == "url"
    assert m["source"]["media"] == "audio"   # défaut : pas de vidéo
    assert m["source"]["duration"] == 12.0


def test_analyze_route_mocked(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(linktool, "probe_url",
                        lambda url: linktool.normalize_info(RAW_INFO, url))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    r = c.post("/api/tool/analyze",
               json={"url": "https://youtube.example/watch?v=abc123"},
               headers=GEST)
    assert r.status_code == 200
    d = r.json()
    assert d["ai"] is False               # pas de clé -> heuristique
    assert d["suggestion"]["artist"] == "U2"
    assert len(d["suggestion"]["tracks"]) == 2
    # réservé aux gestionnaires
    assert c.post("/api/tool/analyze", json={"url": "https://x.example/"},
                  headers=USER).status_code == 403
    # URL invalide
    assert c.post("/api/tool/analyze", json={"url": "notaurl"},
                  headers=GEST).status_code == 400


def test_split_song():
    v = {"title": "Shaka Ponk - House Of The Rising Sun (The Animals) / TARATATA 2024",
         "channel": "MyTaratata"}
    assert linktool.split_song(v) == ("Shaka Ponk",
                                      "House Of The Rising Sun (The Animals)")
    # Medley : les « / » internes sont préservés, seul le suffixe émission tombe.
    v2 = {"title": "Shaka Ponk - Sun / Brutal Pop / Wanna Get Free 2024",
          "channel": ""}
    assert linktool.split_song(v2) == ("Shaka Ponk",
                                       "Sun / Brutal Pop / Wanna Get Free")
    # Format mytaratata avec crédit de reprise : « (The Animals) » conservé,
    # année « (2024) » retirée.
    v3 = {"title": 'Shaka Ponk "House Of The Rising Sun" (The Animals) (2024)',
          "channel": "Taratata"}
    assert linktool.split_song(v3) == ("Shaka Ponk",
                                       "House Of The Rising Sun (The Animals)")


_TARA_HTML = (
    '<div class="jwplayer" data-image="https://mytaratata.com/img/x.jpeg" '
    'data-source="https://videos.mytaratata.com/videos/402/289-1024x576.mp4">'
    '</div><h1>Coldplay &quot;Viva La Vida&quot; (2011)</h1>')


def test_resolve_mytaratata(monkeypatch):
    class R:
        text = _TARA_HTML
        def raise_for_status(self): pass
    monkeypatch.setattr(linktool.requests, "get", lambda *a, **k: R())
    v = linktool.resolve_mytaratata("https://mytaratata.com/taratata/402/coldplay")
    assert v["webpage_url"].endswith("289-1024x576.mp4")   # cible de download
    assert v["title"] == 'Coldplay "Viva La Vida" (2011)'
    assert v["thumbnail"].endswith(".jpeg")
    assert v["extractor"] == "mytaratata"
    # Titre mytaratata « Artiste "Chanson" (année) » -> artiste/titre corrects
    assert linktool.split_song(v) == ("Coldplay", "Viva La Vida")


def test_probe_routes_mytaratata_pages(monkeypatch):
    """probe_url délègue les pages mytaratata au résolveur, pas à yt-dlp."""
    resolved = []
    monkeypatch.setattr(linktool, "resolve_mytaratata",
                        lambda url: resolved.append(url) or {"ok": 1})
    # yt-dlp mocké en échec explicite : le distinguer d'une résolution.
    def stub_ytdlp(*a, **k):
        raise RuntimeError("yt-dlp appelé")
    monkeypatch.setattr(linktool.subprocess, "run", stub_ytdlp)

    # Page mytaratata -> résolveur, pas yt-dlp.
    linktool.probe_url("https://mytaratata.com/taratata/402/x")
    assert resolved == ["https://mytaratata.com/taratata/402/x"]

    # MP4 direct -> yt-dlp générique, jamais le résolveur.
    with pytest.raises(RuntimeError, match="yt-dlp appelé"):
        linktool.probe_url("https://videos.mytaratata.com/videos/402/289.mp4")
    assert resolved == ["https://mytaratata.com/taratata/402/x"]   # inchangé


def test_analyze_multi_route(client, monkeypatch):
    c, _ = client

    def fake_probe(url):
        song = "Sex Ball" if "sex" in url else "Ayo (I'm Picky)"
        return linktool.normalize_info(
            {"title": f"Shaka Ponk - {song} / TARATATA 2024",
             "channel": "MyTaratata", "duration": 200.0,
             "thumbnail": "https://img/x.jpg", "webpage_url": url,
             "extractor_key": "Generic"}, url)

    monkeypatch.setattr(linktool, "probe_url", fake_probe)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    urls = ["https://mytaratata.com/sex-ball", "https://mytaratata.com/ayo"]
    r = c.post("/api/tool/analyze-multi", json={"urls": urls}, headers=GEST)
    assert r.status_code == 200
    d = r.json()
    assert len(d["clips"]) == 2
    assert d["clips"][0]["title"] == "Sex Ball"
    assert d["clips"][0]["clip_artist"] == "Shaka Ponk"
    assert d["suggestion"]["artist"] == "Shaka Ponk"
    assert d["thumbnail"] == "https://img/x.jpg"
    # Lien illisible isolé : n'interrompt pas les autres
    def probe_maybe(url):
        if "bad" in url:
            raise linktool.ProbeError("404")
        return fake_probe(url)
    monkeypatch.setattr(linktool, "probe_url", probe_maybe)
    r2 = c.post("/api/tool/analyze-multi",
                json={"urls": ["https://x/bad", "https://x/sex"]}, headers=GEST)
    assert r2.status_code == 200
    clips = r2.json()["clips"]
    assert clips[0]["error"] and not clips[1].get("error")
    # Réservé aux gestionnaires, liste vide refusée
    assert c.post("/api/tool/analyze-multi", json={"urls": urls},
                  headers=USER).status_code == 403
    assert c.post("/api/tool/analyze-multi", json={"urls": []},
                  headers=GEST).status_code == 400


def test_create_job_multi_manifest(client):
    c, projects = client
    payload = {
        "album": {"artist": "Shaka Ponk", "title": "Taratata - Shaka Ponk"},
        "tracks": [],
        "clips": [
            {"url": "https://t/1", "title": "Sex Ball", "duration": 200.0},
            {"url": "https://t/2", "title": "Ayo", "artist": "Ayo", "duration": 180.0},
        ],
        "video": True,   # ignoré en multi : audio forcé
    }
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    m = c.get(f"/api/jobs/{slug}/manifest", headers=GEST).json()
    assert m["source"]["multi"] is True
    assert [c2["url"] for c2 in m["source"]["clips"]] == ["https://t/1", "https://t/2"]
    assert m["source"]["media"] == "audio"        # vidéo désactivée en multi
    assert m["auto_setlist"] is False
    assert len(m["tracks"]) == 2
    assert m["tracks"][0]["title"] == "Sex Ball" and m["tracks"][0]["locked"] is True
    assert m["tracks"][1]["artist"] == "Ayo"
    # Timecodes posés seulement à la préparation (concaténation)
    assert m["tracks"][0]["start"] is None


def test_run_multi_concat_and_timecodes(client, monkeypatch):
    """Concaténation réelle de 2 clips synthétiques -> master + timecodes."""
    from backend import main
    from backend.manifest import Manifest
    from backend.pipeline import download
    c, projects = client
    payload = {
        "album": {"artist": "Shaka Ponk", "title": "Taratata - Shaka Ponk"},
        "tracks": [],
        "clips": [
            {"url": "https://t/1", "title": "A", "duration": 3.0},
            {"url": "https://t/2", "title": "B", "duration": 5.0},
        ],
    }
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    project_dir = projects / slug

    # download_audio mocké : produit un fichier audio synthétique par clip
    # (durées volontairement différentes pour vérifier le bornage cumulé).
    durs = {0: 3.0, 1: 5.0}
    def fake_dl(url, source_dir, cookies=None, progress=None, stem="master_audio"):
        idx = int(stem.split("_")[1])
        # Extension distincte du .wav produit par extract_wav (comme le ferait
        # yt-dlp : m4a/webm), sinon entrée == sortie à l'extraction.
        out = source_dir / f"{stem}.src.wav"
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={durs[idx]}",
            "-ar", "44100", "-ac", "2", str(out)], check=True, capture_output=True)
        if progress:
            progress(100.0)
        return out
    monkeypatch.setattr(download, "download_audio", fake_dl)

    download.run(project_dir)

    master = project_dir / "source" / "master.wav"
    assert master.exists()
    assert abs(download._wav_seconds(master) - 8.0) < 0.1   # 3 + 5
    m = Manifest.load(project_dir / "manifest.yaml")
    assert m.state("download") == "done"
    t0, t1 = m.tracks
    assert abs(t0["start"] - 0.0) < 0.05 and abs(t0["end"] - 3.0) < 0.05
    assert abs(t1["start"] - 3.0) < 0.05 and abs(t1["end"] - 8.0) < 0.05
    assert t0["locked"] and t1["locked"]
    # Segments temporaires nettoyés
    assert not list((project_dir / "source").glob("clip_*.wav"))


def test_reorder_master_multi(client, monkeypatch):
    """Réordonnancement : re-concaténation du master + timecodes recalculés."""
    from backend.manifest import Manifest
    from backend.pipeline import download
    c, projects = client
    payload = {
        "album": {"artist": "Coldplay", "title": "Taratata - Coldplay"},
        "tracks": [],
        "clips": [{"url": "https://t/1", "title": "A", "duration": 3.0},
                  {"url": "https://t/2", "title": "B", "duration": 5.0}],
    }
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    project_dir = projects / slug
    durs = {0: 3.0, 1: 5.0}
    def fake_dl(url, source_dir, cookies=None, progress=None, stem="master_audio"):
        idx = int(stem.split("_")[1])
        out = source_dir / f"{stem}.src.wav"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                        "-i", f"sine=frequency=440:duration={durs[idx]}",
                        "-ar", "44100", "-ac", "2", str(out)],
                       check=True, capture_output=True)
        return out
    monkeypatch.setattr(download, "download_audio", fake_dl)
    download.run(project_dir)

    # Cas anomal : la liste du manifeste n'est PAS dans l'ordre temporel
    # (piste B avant A dans la liste, mais A commence à 0). La permutation
    # reçue porte sur l'ordre TEMPOREL (comme l'éditeur), pas l'ordre de liste.
    m0 = Manifest.load(project_dir / "manifest.yaml")
    m0.data["tracks"] = list(reversed(m0.data["tracks"]))
    m0.data["source"]["clips"] = list(reversed(m0.data["source"]["clips"]))
    m0.save()

    # order=[1,0] sur l'ordre temporel (A=idx0 @0-3, B=idx1 @3-8) -> B en tête.
    res = download.reorder_master(project_dir, [1, 0])
    assert res["tracks"] == 2
    m = Manifest.load(project_dir / "manifest.yaml")
    t0, t1 = m.tracks
    assert t0["title"] == "B" and t0["n"] == 1
    assert abs(t0["start"] - 0.0) < 0.05 and abs(t0["end"] - 5.0) < 0.05
    assert t1["title"] == "A" and t1["n"] == 2
    assert abs(t1["start"] - 5.0) < 0.05 and abs(t1["end"] - 8.0) < 0.05
    # clips réordonnés en parallèle ; master toujours 8 s ; segments nettoyés
    assert [cl["url"] for cl in m.data["source"]["clips"]] == ["https://t/2", "https://t/1"]
    assert abs(download._wav_seconds(project_dir / "source" / "master.wav") - 8.0) < 0.1
    assert not list((project_dir / "source").glob("reorder_*.wav"))
    # Permutation invalide rejetée
    with pytest.raises(ValueError):
        download.reorder_master(project_dir, [0, 0])


def test_reorder_endpoint_rejects_single_source(client):
    """L'endpoint /reorder refuse un album mono-source (400)."""
    c, projects = client
    slug = c.post("/api/jobs", json=_job_payload(), headers=GEST).json()["slug"]
    r = c.post(f"/api/jobs/{slug}/reorder", json={"order": [0]}, headers=GEST)
    assert r.status_code == 400


def test_setlist_replace_and_validation(client):
    c, projects = client
    slug = c.post("/api/jobs", json=_job_payload(), headers=GEST).json()["slug"]
    # remplace par 2 pistes livrées dans le désordre -> triées, renumérotées
    tracks = [{"n": 5, "title": "B", "start": 6.0, "end": 12.0},
              {"n": 2, "title": "A", "artist": "Guest", "start": 0.0, "end": 6.0}]
    r = c.put(f"/api/jobs/{slug}/setlist", json={"tracks": tracks}, headers=GEST)
    assert r.status_code == 200
    m = c.get(f"/api/jobs/{slug}/manifest", headers=GEST).json()
    assert [t["title"] for t in m["tracks"]] == ["A", "B"]
    assert m["tracks"][0]["artist"] == "Guest"
    assert all(t["locked"] for t in m["tracks"])
    # validations
    assert c.put(f"/api/jobs/{slug}/setlist", json={"tracks": []},
                 headers=GEST).status_code == 400
    bad = [{"n": 1, "title": "X", "start": 5.0, "end": 2.0}]
    assert c.put(f"/api/jobs/{slug}/setlist", json={"tracks": bad},
                 headers=GEST).status_code == 400


def test_job_audio_route(client):
    c, projects = client
    slug = c.post("/api/jobs", json=_job_payload(), headers=GEST).json()["slug"]
    assert c.get(f"/api/jobs/{slug}/audio", headers=GEST).status_code == 404
    preview = projects / slug / "source" / "preview.mp3"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 64)
    r = c.get(f"/api/jobs/{slug}/audio", headers=GEST)
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"


def test_delete_job_draft_only(client):
    c, projects = client
    slug = c.post("/api/jobs", json=_job_payload(), headers=GEST).json()["slug"]
    audio = projects / slug / "build" / "audio"
    audio.mkdir(parents=True)
    (audio / "01. A.mp3").write_bytes(b"x")
    assert c.delete(f"/api/jobs/{slug}", headers=GEST).status_code == 409
    (audio / "01. A.mp3").unlink()
    assert c.delete(f"/api/jobs/{slug}", headers=GEST).status_code == 200
    assert not (projects / slug).exists()


# --- Préparation : timecodes déjà connus => pas de whisper -----------------
def test_prepare_skips_ai_when_timecodes_present(client, monkeypatch, tmp_path):
    from backend import main
    from backend.pipeline import preanalyze
    c, projects = client
    payload = _job_payload(tracks=[
        {"n": 1, "title": "A", "start": 0.0, "end": 6.0},
        {"n": 2, "title": "B", "start": 6.0, "end": 12.0}])
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    project_dir = projects / slug

    # master déjà téléchargé (state done + wav présent)
    (project_dir / "source").mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
        "-ar", "44100", "-ac", "2",
        str(project_dir / "source" / "master.wav")],
        check=True, capture_output=True)
    from backend.manifest import Manifest
    m = Manifest.load(project_dir / "manifest.yaml")
    m.set_state("download", "done")

    def boom(*a, **k):
        raise AssertionError("whisper ne doit pas être appelé")
    monkeypatch.setattr(preanalyze, "transcribe", boom)

    progress.reset(slug)
    main._run_prepare_bg(slug, "g")

    events = progress.history(slug)
    assert any(e["stage"] == "prepare" and e["status"] == "complete"
               for e in events), events
    assert (project_dir / "source" / "waveform.dat").exists()
    assert (project_dir / "source" / "preview.mp3").exists()
    m = Manifest.load(project_dir / "manifest.yaml")
    assert m.state("ai_markers") == "done"


def test_prepare_publishes_transcription_progress(client, monkeypatch, tmp_path):
    """La transcription doit émettre des events pct pour la barre de progression."""
    from backend import llm, main
    from backend.pipeline import preanalyze
    c, projects = client
    # Setlist connue mais sans timecodes -> passe par whisper + request_markers.
    payload = _job_payload(tracks=[
        {"n": 1, "title": "A"}, {"n": 2, "title": "B"}])
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    project_dir = projects / slug
    (project_dir / "source").mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
        "-ar", "44100", "-ac", "2",
        str(project_dir / "source" / "master.wav")],
        check=True, capture_output=True)
    from backend.manifest import Manifest
    Manifest.load(project_dir / "manifest.yaml").set_state("download", "done")

    monkeypatch.setattr(boundaries, "detect", lambda *a, **k: [])

    def fake_transcribe(wav, progress=None):
        # Simule la progression segment par segment.
        for frac in (0.25, 0.5, 0.75, 1.0):
            if progress:
                progress(frac)
        return [{"start": 0.0, "end": 6.0, "text": "a"},
                {"start": 6.0, "end": 12.0, "text": "b"}]
    monkeypatch.setattr(preanalyze, "transcribe", fake_transcribe)
    monkeypatch.setattr(llm, "request_markers", lambda *a, **k: {
        1: {"start": 0.0, "end": 6.0}, 2: {"start": 6.0, "end": 12.0}})

    progress.reset(slug)
    main._run_prepare_bg(slug, "g")

    events = progress.history(slug)
    pct_events = [e for e in events
                  if e["stage"] == "ai_markers"
                  and e["info"].get("phase") == "transcription"
                  and e["info"].get("pct") is not None]
    assert pct_events, events
    pcts = [e["info"]["pct"] for e in pct_events]
    assert pcts == sorted(pcts)          # progression monotone
    assert max(pcts) == 100.0            # atteint la fin
    assert any(e["stage"] == "prepare" and e["status"] == "complete"
               for e in events)


def _seed_prepared(c, projects, tracks):
    """Projet avec master.wav et download=done, prêt pour _run_prepare_bg."""
    slug = c.post("/api/jobs", json=_job_payload(tracks=tracks),
                  headers=GEST).json()["slug"]
    project_dir = projects / slug
    (project_dir / "source").mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
        "-ar", "44100", "-ac", "2",
        str(project_dir / "source" / "master.wav")],
        check=True, capture_output=True)
    from backend.manifest import Manifest
    Manifest.load(project_dir / "manifest.yaml").set_state("download", "done")
    return slug, project_dir


def test_skip_detection_before_start(client, monkeypatch):
    """Skip demandé avant la transcription : whisper n'est jamais appelé."""
    from backend import main
    from backend.pipeline import preanalyze
    from backend.manifest import Manifest
    c, projects = client
    slug, project_dir = _seed_prepared(
        c, projects, [{"n": 1, "title": "A"}, {"n": 2, "title": "B"}])
    monkeypatch.setattr(boundaries, "detect", lambda *a, **k: [])

    def boom(*a, **k):
        raise AssertionError("whisper ne doit pas être appelé après un skip")
    monkeypatch.setattr(preanalyze, "transcribe", boom)

    assert c.post(f"/api/jobs/{slug}/skip-detection",
                  headers=GEST).status_code == 200
    progress.reset(slug)
    main._run_prepare_bg(slug, "g")

    events = progress.history(slug)
    done = [e for e in events
            if e["stage"] == "ai_markers" and e["status"] == "done"]
    assert done and done[0]["info"]["source"] == "skipped", events
    # Des coupes de secours sont posées : l'éditeur peut s'ouvrir.
    m = Manifest.load(project_dir / "manifest.yaml")
    assert all(t["start"] is not None and t["end"] is not None for t in m.tracks)
    assert slug not in main._skip_detection      # drapeau consommé
    assert any(e["stage"] == "prepare" and e["status"] == "complete"
               for e in events)


def test_skip_detection_during_transcription(client, monkeypatch):
    """Skip pendant la transcription : interrompue au segment suivant."""
    from backend import main
    from backend.pipeline import preanalyze
    c, projects = client
    slug, _ = _seed_prepared(c, projects, [{"n": 1, "title": "A"}])
    monkeypatch.setattr(boundaries, "detect", lambda *a, **k: [])
    seen = []

    def fake_transcribe(wav, progress=None):
        progress(0.1)                       # 1er segment : poursuit
        seen.append("running")
        main._skip_detection.add(slug)      # l'utilisateur clique « passer »
        progress(0.2)                       # doit lever _SkipDetection
        raise AssertionError("la transcription aurait dû être interrompue")
    monkeypatch.setattr(preanalyze, "transcribe", fake_transcribe)

    progress.reset(slug)
    main._run_prepare_bg(slug, "g")

    assert seen == ["running"]
    done = [e for e in progress.history(slug)
            if e["stage"] == "ai_markers" and e["status"] == "done"]
    assert done and done[0]["info"]["source"] == "skipped"
    assert slug not in main._skip_detection


def test_skip_detection_auth_and_404(client):
    c, projects = client
    slug = c.post("/api/jobs", json=_job_payload(), headers=GEST).json()["slug"]
    assert c.post(f"/api/jobs/{slug}/skip-detection").status_code == 401
    assert c.post(f"/api/jobs/{slug}/skip-detection",
                  headers=USER).status_code == 403
    assert c.post("/api/jobs/inconnu-2020-01-01/skip-detection",
                  headers=GEST).status_code == 404


# --- Tags : artiste par piste ----------------------------------------------
def test_tag_mp3_track_artist(tmp_path):
    mp3 = tmp_path / "01. Beautiful Day.mp3"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:a", "libmp3lame", "-q:a", "9", str(mp3)],
        check=True, capture_output=True)
    track = {"n": 1, "title": "Beautiful Day", "artist": "U2 with Chris Martin"}
    album = {"artist": "U2", "title": "Live in Times Square 2014",
             "date": "2014-12-01"}
    tag_mp3(mp3, track, album, total=4, cover=None, cover_mime="")
    from mutagen.id3 import ID3
    tags = ID3(str(mp3))
    assert str(tags["TPE1"].text[0]) == "U2 with Chris Martin"
    assert str(tags["TPE2"].text[0]) == "U2"


def test_track_filename_library_format():
    from backend.manifest import Manifest
    m = Manifest(data={"album": {"artist": "a", "title": "t"},
                       "target": "data_disc",
                       "tracks": [{"n": 1, "title": "AC/DC: Redux?"}]})
    assert m.track_filename(m.data["tracks"][0], "mp3") == "01. ACDC Redux.mp3"


# --- Publication : brouillon par défaut, visibilité par rôle ---------------
def test_tool_album_starts_unpublished_and_publish_flow(client):
    c, projects = client
    slug = c.post("/api/jobs", json=_job_payload(), headers=GEST).json()["slug"]
    m = c.get(f"/api/jobs/{slug}/manifest", headers=GEST).json()
    assert m["published"] is False

    # rendu simulé : un MP3 suffit pour être candidat au catalogue
    audio = projects / slug / "build" / "audio"
    audio.mkdir(parents=True)
    (audio / "01. A.mp3").write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 64)

    # public / user simple : l'album n'existe pas
    assert all(a["slug"] != slug for a in c.get("/api/catalogue").json())
    assert c.get(f"/api/catalogue/{slug}").status_code == 404
    assert c.get(f"/api/catalogue/{slug}", headers=USER).status_code == 404
    assert c.get(f"/download/{slug}/track/1", headers=USER).status_code == 404
    assert c.get(f"/download/{slug}/mp3", headers=USER).status_code == 404
    assert c.get(f"/api/social/albums/{slug}/covers",
                 headers=USER).status_code == 404
    assert c.get(f"/api/social/albums/{slug}/comments",
                 headers=USER).status_code == 404

    # gestionnaire : visible partout, drapeau published exposé
    entry = [a for a in c.get("/api/catalogue", headers=GEST).json()
             if a["slug"] == slug]
    assert entry and entry[0]["published"] is False
    assert c.get(f"/api/catalogue/{slug}", headers=GEST).json()["published"] is False
    assert c.get(f"/download/{slug}/track/1", headers=GEST).status_code == 200

    # publication -> visible du public
    r = c.patch(f"/api/albums/{slug}/published", json={"published": True},
                headers=GEST)
    assert r.status_code == 200
    assert c.get(f"/api/catalogue/{slug}").status_code == 200
    assert c.get(f"/download/{slug}/track/1", headers=USER).status_code == 200
