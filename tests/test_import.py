"""Import d'un album prêt : lecture des métadonnées, staging, commit."""
from __future__ import annotations

import io
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TRCK

GEST = {"X-authentik-username": "g", "X-authentik-groups": "live2mp3-gestionnaire"}
USER = {"X-authentik-username": "u", "X-authentik-groups": "live2mp3-user"}

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Même isolation que test_t9_api : chaque module a importé PROJECTS_DIR
    # par valeur, il faut donc les patcher un par un.
    from backend import catalogue, covers, db, import_album, main, manifest, social
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "live2mp3.db")
    monkeypatch.setattr(db, "AVATARS_DIR", data / "avatars")
    monkeypatch.setattr(social, "AVATARS_DIR", data / "avatars")
    for mod in (manifest, main, catalogue, social, covers, import_album):
        monkeypatch.setattr(mod, "PROJECTS_DIR", tmp_path)
    db.init_db()
    return TestClient(main.app), tmp_path


def _mp3(path: Path, seconds: float = 0.5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-codec:a", "libmp3lame", "-b:a", "32k", str(path),
    ], check=True, capture_output=True)
    return path


def _tag(path: Path, *, n: int, title: str, artist: str, album: str,
         date: str, cover: bytes | None = None) -> None:
    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=artist))
    tags.add(TPE2(encoding=3, text=artist))
    tags.add(TALB(encoding=3, text=album))
    tags.add(TDRC(encoding=3, text=date))
    tags.add(TRCK(encoding=3, text=str(n)))
    if cover:
        tags.add(APIC(encoding=3, mime="image/png", type=3, desc="Cover", data=cover))
    tags.save(str(path))


def _tagged_album(tmp_path: Path, n_tracks: int = 3, cover: bytes | None = None) -> list[Path]:
    titles = ["Overcompensate", "Next Semester", "Backslide"]
    out = []
    src = tmp_path / "src"
    for i in range(1, n_tracks + 1):
        p = _mp3(src / f"{i:02d}_{titles[i - 1].replace(' ', '_')}.mp3")
        _tag(p, n=i, title=titles[i - 1], artist="Twenty One Pilots",
             album="Clancy Live", date="2024-08-15", cover=cover)
        out.append(p)
    return out


def _upload(paths: list[Path]) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("files", (p.name, p.read_bytes(), "audio/mpeg")) for p in paths]


# ── Analyse ────────────────────────────────────────────────────────────────

def test_analyze_prefills_from_tags(client, tmp_path):
    c, _ = client
    r = c.post("/api/import/analyze", headers=GEST, files=_upload(_tagged_album(tmp_path)))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["album"] == {"artist": "Twenty One Pilots", "title": "Clancy Live",
                          "date": "2024-08-15", "venue": "", "festival": ""}
    assert [(t["n"], t["title"]) for t in d["tracks"]] == [
        (1, "Overcompensate"), (2, "Next Semester"), (3, "Backslide")]
    assert d["slug"] == "twenty-one-pilots-2024-08-15"
    assert d["slug_exists"] is False


def test_analyze_falls_back_to_filename_without_tags(client, tmp_path):
    c, _ = client
    paths = [_mp3(tmp_path / "src" / "01. Pour Me.mp3"),
             _mp3(tmp_path / "src" / "02. Vignette.mp3")]
    r = c.post("/api/import/analyze", headers=GEST, files=_upload(paths))
    d = r.json()
    assert [(t["n"], t["title"]) for t in d["tracks"]] == [(1, "Pour Me"), (2, "Vignette")]
    # Sans tags il n'y a pas d'artiste : le formulaire doit être complété à la main.
    assert d["album"]["artist"] == ""
    assert any("métadonnées" in w for w in d["warnings"])


def test_analyze_accepts_zip(client, tmp_path):
    c, _ = client
    paths = _tagged_album(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in paths:
            zf.write(p, f"Clancy Live/{p.name}")
        zf.writestr("Clancy Live/cover.png", PNG_1PX)
        zf.writestr("__MACOSX/._junk", b"x")   # parasite : doit être ignoré
    r = c.post("/api/import/analyze", headers=GEST,
               files=[("files", ("album.zip", buf.getvalue(), "application/zip"))])
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["tracks"]) == 3
    assert d["cover"] == "cover.png"
    assert d["cover_source"] == "file"


def test_analyze_rejects_zip_slip(client, tmp_path):
    c, _ = client
    p = _tagged_album(tmp_path, n_tracks=1)[0]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.write(p, "../../../evil.mp3")
        zf.write(p, "01_ok.mp3")
    r = c.post("/api/import/analyze", headers=GEST,
               files=[("files", ("a.zip", buf.getvalue(), "application/zip"))])
    assert r.status_code == 200
    # Le membre échappé est aplati sur son basename, jamais écrit hors staging.
    assert not (tmp_path.parent / "evil.mp3").exists()
    files = [t["file"] for t in r.json()["tracks"]]
    assert all("/" not in f and ".." not in f for f in files)


def test_analyze_extracts_embedded_cover(client, tmp_path):
    c, _ = client
    r = c.post("/api/import/analyze", headers=GEST,
               files=_upload(_tagged_album(tmp_path, cover=PNG_1PX)))
    d = r.json()
    assert d["cover_source"] == "embedded"
    assert d["cover"].startswith("cover")


def test_staging_file_serves_cover_preview(client, tmp_path):
    c, _ = client
    a = c.post("/api/import/analyze", headers=GEST,
               files=_upload(_tagged_album(tmp_path, cover=PNG_1PX))).json()
    r = c.get(f"/api/import/{a['token']}/file/{a['cover']}", headers=GEST)
    assert r.status_code == 200
    assert r.content == PNG_1PX
    # L'aperçu ne sert que des images, et jamais hors du staging.
    assert c.get(f"/api/import/{a['token']}/file/{a['tracks'][0]['file']}",
                 headers=GEST).status_code == 403
    assert c.get(f"/api/import/{a['token']}/file/..%2F..%2Fmanifest.yaml",
                 headers=GEST).status_code == 404
    assert c.get(f"/api/import/{a['token']}/file/{a['cover']}").status_code == 401


def test_analyze_requires_gestionnaire(client, tmp_path):
    c, _ = client
    files = _upload(_tagged_album(tmp_path, n_tracks=1))
    assert c.post("/api/import/analyze", files=files).status_code == 401
    assert c.post("/api/import/analyze", headers=USER, files=files).status_code == 403


def test_analyze_without_mp3_is_rejected(client, tmp_path):
    c, _ = client
    r = c.post("/api/import/analyze", headers=GEST,
               files=[("files", ("cover.png", PNG_1PX, "image/png"))])
    assert r.status_code == 400


# ── Commit ─────────────────────────────────────────────────────────────────

def _commit_payload(analyzed: dict, **over) -> dict:
    p = {"token": analyzed["token"], "artist": analyzed["album"]["artist"],
         "title": analyzed["album"]["title"], "date": analyzed["album"]["date"],
         "venue": "Accor Arena", "festival": "", "published": True,
         "cover": analyzed["cover"], "traycard": analyzed["traycard"],
         "tracks": [{"n": t["n"], "title": t["title"], "file": t["file"]}
                    for t in analyzed["tracks"]]}
    p.update(over)
    return p


def test_commit_creates_album(client, tmp_path):
    c, projects = client
    a = c.post("/api/import/analyze", headers=GEST,
               files=_upload(_tagged_album(tmp_path, cover=PNG_1PX))).json()
    r = c.post("/api/import/commit", headers=GEST, json=_commit_payload(a))
    assert r.status_code == 200, r.text
    d = r.json()
    slug = d["slug"]
    assert slug == "twenty-one-pilots-2024-08-15"

    # Fichiers au format de nommage du site.
    audio = sorted(p.name for p in (projects / slug / "build" / "audio").glob("*.mp3"))
    assert audio == ["01. Overcompensate.mp3", "02. Next Semester.mp3",
                     "03. Backslide.mp3"]

    # L'album sort dans le catalogue, crédité à l'importateur.
    cat = c.get("/api/catalogue", headers=GEST).json()
    entry = next(x for x in cat if x["slug"] == slug)
    assert entry["imported_by"] == "g"
    assert entry["venue"] == "Accor Arena"
    assert entry["tracks"] == 3
    assert entry["has_cover"] is True

    # Le staging est purgé.
    assert not (projects / ".l2m-import").exists() or \
        not any((projects / ".l2m-import").iterdir())


def test_commit_registers_cover_as_proposal(client, tmp_path):
    c, _ = client
    a = c.post("/api/import/analyze", headers=GEST,
               files=_upload(_tagged_album(tmp_path, cover=PNG_1PX))).json()
    d = c.post("/api/import/commit", headers=GEST, json=_commit_payload(a)).json()
    assert d["cover_id"] is not None
    # La pochette importée est une proposition comme une autre, créditée.
    covers = c.get(f"/api/social/albums/{d['slug']}/covers", headers=GEST).json()
    assert len(covers["covers"]) == 1
    assert covers["covers"][0]["username"] == "g"


def test_commit_writes_tags_and_cover_into_mp3(client, tmp_path):
    c, projects = client
    a = c.post("/api/import/analyze", headers=GEST,
               files=_upload(_tagged_album(tmp_path, cover=PNG_1PX))).json()
    d = c.post("/api/import/commit", headers=GEST,
               json=_commit_payload(a, artist="TØP", title="Clancy")).json()
    mp3 = projects / d["slug"] / "build" / "audio" / "01. Overcompensate.mp3"
    tags = ID3(str(mp3))
    # Les corrections du formulaire priment sur les tags d'origine.
    assert tags["TPE1"].text[0] == "TØP"
    assert tags["TALB"].text[0] == "Clancy"
    assert tags["TIT2"].text[0] == "01. Overcompensate"
    assert tags["TRCK"].text[0] == "1/3"
    assert tags.getall("APIC")


def test_commit_refuses_duplicate_slug(client, tmp_path):
    c, _ = client
    a1 = c.post("/api/import/analyze", headers=GEST,
                files=_upload(_tagged_album(tmp_path))).json()
    assert c.post("/api/import/commit", headers=GEST,
                  json=_commit_payload(a1)).status_code == 200
    a2 = c.post("/api/import/analyze", headers=GEST,
                files=_upload(_tagged_album(tmp_path))).json()
    assert a2["slug_exists"] is True
    r = c.post("/api/import/commit", headers=GEST, json=_commit_payload(a2))
    assert r.status_code == 409


def test_commit_unpublished_stays_draft(client, tmp_path):
    c, _ = client
    a = c.post("/api/import/analyze", headers=GEST,
               files=_upload(_tagged_album(tmp_path))).json()
    d = c.post("/api/import/commit", headers=GEST,
               json=_commit_payload(a, published=False)).json()
    assert [x["slug"] for x in c.get("/api/catalogue").json()
            if x["slug"] == d["slug"]] == []
    assert any(x["slug"] == d["slug"]
               for x in c.get("/api/catalogue", headers=GEST).json())


def test_commit_requires_gestionnaire(client, tmp_path):
    c, _ = client
    a = c.post("/api/import/analyze", headers=GEST,
               files=_upload(_tagged_album(tmp_path, n_tracks=1))).json()
    assert c.post("/api/import/commit", headers=USER,
                  json=_commit_payload(a)).status_code == 403


def test_cancel_purges_staging(client, tmp_path):
    c, projects = client
    a = c.post("/api/import/analyze", headers=GEST,
               files=_upload(_tagged_album(tmp_path, n_tracks=1))).json()
    assert (projects / ".l2m-import" / a["token"]).exists()
    assert c.post("/api/import/cancel", headers=GEST,
                  json={"token": a["token"]}).status_code == 200
    assert not (projects / ".l2m-import" / a["token"]).exists()


# ── Upload chunké résumable (gros audio/vidéo) ──────────────────────────────

def _chunked_upload(c, name, data, *, token=None, chunk=64):
    """Téléverse `data` en morceaux de `chunk` octets, avec vérif de reprise."""
    init = c.post("/api/import/upload/init", headers=GEST,
                  json={"filename": name, "size": len(data),
                        **({"token": token} if token else {})})
    assert init.status_code == 200, init.text
    tok, fid = init.json()["token"], init.json()["file_id"]
    off = 0
    while off < len(data):
        part = data[off:off + chunk]
        r = c.put(f"/api/import/upload/{tok}/{fid}", headers={**GEST, "X-Chunk-Offset": str(off)},
                  content=part)
        assert r.status_code == 200, r.text
        off += len(part)
        assert r.json()["received"] == off
    fin = c.post(f"/api/import/upload/{tok}/{fid}/finish", headers=GEST,
                 json={"size": len(data)})
    assert fin.status_code == 200, fin.text
    return tok, fin.json()["file"]


def test_chunked_upload_then_analyze_mp3(client, tmp_path):
    c, projects = client
    p = _mp3(tmp_path / "src" / "01_Song.mp3")
    _tag(p, n=1, title="Song", artist="TOP", album="Live", date="2024-01-01")
    data = p.read_bytes()
    tok, fname = _chunked_upload(c, "01_Song.mp3", data)
    assert (projects / ".l2m-import" / tok / "files" / fname).exists()
    a = c.post("/api/import/analyze-staged", headers=GEST, json={"token": tok}).json()
    assert a["token"] == tok
    assert len(a["tracks"]) == 1
    assert a["has_video"] is False


def test_chunked_upload_resume_after_interruption(client):
    c, _ = client
    data = b"A" * 500
    init = c.post("/api/import/upload/init", headers=GEST,
                  json={"filename": "clip.mp4", "size": len(data)}).json()
    tok, fid = init["token"], init["file_id"]
    # Premier morceau, puis "coupure" : on interroge l'état pour reprendre.
    c.put(f"/api/import/upload/{tok}/{fid}", headers={**GEST, "X-Chunk-Offset": "0"},
          content=data[:200])
    st = c.get(f"/api/import/upload/{tok}/{fid}", headers=GEST).json()
    assert st["received"] == 200 and st["size"] == 500
    # Reprise à l'octet reçu.
    r = c.put(f"/api/import/upload/{tok}/{fid}", headers={**GEST, "X-Chunk-Offset": "200"},
              content=data[200:])
    assert r.json()["received"] == 500


def test_chunked_upload_rejects_desynced_offset(client):
    c, _ = client
    init = c.post("/api/import/upload/init", headers=GEST,
                  json={"filename": "clip.mp4", "size": 300}).json()
    tok, fid = init["token"], init["file_id"]
    c.put(f"/api/import/upload/{tok}/{fid}", headers={**GEST, "X-Chunk-Offset": "0"},
          content=b"X" * 100)
    # Offset erroné : refus 409 avec l'octet réellement reçu.
    r = c.put(f"/api/import/upload/{tok}/{fid}", headers={**GEST, "X-Chunk-Offset": "999"},
              content=b"Y" * 50)
    assert r.status_code == 409
    assert r.json()["detail"]["received"] == 100


def test_chunked_upload_rejects_bad_extension(client):
    c, _ = client
    r = c.post("/api/import/upload/init", headers=GEST,
               json={"filename": "notes.txt", "size": 10})
    assert r.status_code == 400


def test_chunked_upload_requires_gestionnaire(client):
    c, _ = client
    r = c.post("/api/import/upload/init", headers=USER,
               json={"filename": "clip.mp4", "size": 10})
    assert r.status_code == 403


def test_chunked_second_file_same_staging(client, tmp_path):
    c, projects = client
    p = _mp3(tmp_path / "src" / "a.mp3")
    _tag(p, n=1, title="A", artist="TOP", album="Live", date="2024-01-01")
    tok, _ = _chunked_upload(c, "01_A.mp3", p.read_bytes())
    q = _mp3(tmp_path / "src" / "b.mp3")
    _tag(q, n=2, title="B", artist="TOP", album="Live", date="2024-01-01")
    tok2, _ = _chunked_upload(c, "02_B.mp3", q.read_bytes(), token=tok)
    assert tok2 == tok
    a = c.post("/api/import/analyze-staged", headers=GEST, json={"token": tok}).json()
    assert len(a["tracks"]) == 2


# ── Import complet + découpe IA (prepare-ai) ────────────────────────────────

def test_prepare_ai_with_setlistfm(client, tmp_path, monkeypatch):
    """Fichier complet téléversé → projet créé, master prêt, setlist officielle
    pré-remplie, pipeline IA lancé (neutralisé ici)."""
    c, projects = client
    from backend import main as bmain, setlistfm
    started = {}
    monkeypatch.setattr(bmain, "_run_prepare_bg",
                        lambda slug, user: started.update(slug=slug, user=user))
    monkeypatch.setattr(setlistfm, "lookup", lambda artist, date: {
        "url": "https://www.setlist.fm/setlist/top/2024/x-abcdef12.html",
        "venue": "Scottrade Center", "tour": "Clancy",
        "tracks": [{"n": 1, "title": "Overcompensate", "artist": None},
                   {"n": 2, "title": "Holding On to You", "artist": None}]})
    p = _mp3(tmp_path / "src" / "concert.mp3", seconds=0.6)
    tok, fname = _chunked_upload(c, "concert.mp3", p.read_bytes())
    r = c.post("/api/import/prepare-ai", headers=GEST, json={
        "token": tok, "file": fname, "artist": "Twenty One Pilots",
        "date": "2024-08-15"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["setlist_source"] == "setlistfm"
    assert d["auto_setlist"] is False and d["tracks"] == 2
    assert started["slug"] == d["slug"]           # pipeline IA déclenché
    man = c.get(f"/api/jobs/{d['slug']}/manifest", headers=GEST).json()
    assert man["pipeline_state"]["download"] == "done"
    assert man["source"]["media"] == "audio"
    assert man["source"]["duration"] > 0
    assert man["meta"]["setlistfm_url"].endswith(".html")
    assert len(man["tracks"]) == 2
    assert (projects / d["slug"] / "source" / "master.wav").exists()
    assert not (projects / ".l2m-import" / tok).exists()   # staging purgé


def test_prepare_ai_auto_setlist_when_no_match(client, tmp_path, monkeypatch):
    c, _ = client
    from backend import main as bmain, setlistfm
    monkeypatch.setattr(bmain, "_run_prepare_bg", lambda slug, user: None)
    monkeypatch.setattr(setlistfm, "lookup", lambda artist, date: None)
    p = _mp3(tmp_path / "src" / "c2.mp3", seconds=0.4)
    tok, fname = _chunked_upload(c, "c2.mp3", p.read_bytes())
    r = c.post("/api/import/prepare-ai", headers=GEST, json={
        "token": tok, "file": fname, "artist": "Obscure Band", "date": "1999-01-01"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["setlist_source"] == "ai" and d["auto_setlist"] is True
    man = c.get(f"/api/jobs/{d['slug']}/manifest", headers=GEST).json()
    assert man["auto_setlist"] is True


def test_prepare_ai_requires_artist(client, tmp_path):
    c, _ = client
    p = _mp3(tmp_path / "src" / "c3.mp3", seconds=0.3)
    tok, fname = _chunked_upload(c, "c3.mp3", p.read_bytes())
    r = c.post("/api/import/prepare-ai", headers=GEST,
               json={"token": tok, "file": fname, "artist": "  "})
    assert r.status_code == 400


def test_prepare_ai_uses_setlistfm_url(client, tmp_path, monkeypatch):
    """URL setlist.fm fournie à la main : branche lookup_by_url prioritaire."""
    c, _ = client
    from backend import main as bmain, setlistfm
    monkeypatch.setattr(bmain, "_run_prepare_bg", lambda slug, user: None)
    seen = {}
    def fake_by_url(url):
        seen["url"] = url
        return {"url": url, "venue": "V", "tour": "T",
                "tracks": [{"n": 1, "title": "Song", "artist": None}]}
    monkeypatch.setattr(setlistfm, "lookup_by_url", fake_by_url)
    p = _mp3(tmp_path / "src" / "c4.mp3", seconds=0.3)
    tok, fname = _chunked_upload(c, "c4.mp3", p.read_bytes())
    url = "https://www.setlist.fm/setlist/top/2021/x-1a2b3c4d.html"
    r = c.post("/api/import/prepare-ai", headers=GEST, json={
        "token": tok, "file": fname, "artist": "TOP", "setlistfm_url": url})
    assert r.status_code == 200, r.text
    assert seen["url"] == url
    assert r.json()["setlist_source"] == "setlistfm"
