"""Archives temporaires orphelines nettoyées, celles en cours épargnées."""
import os
import time


def test_purge_ne_touche_que_les_vieux_fichiers_de_l_app(tmp_path):
    from backend.main import purge_stale_tempfiles
    old = time.time() - 7 * 3600
    stale = tmp_path / "l2m_bulk_abc.zip"; stale.write_bytes(b"x")
    stale_dir = tmp_path / "bundle-slug-x"; stale_dir.mkdir()
    fresh = tmp_path / "l2m_art_new.zip"; fresh.write_bytes(b"x")
    foreign = tmp_path / "autre_appli.zip"; foreign.write_bytes(b"x")
    for p in (stale, stale_dir, foreign):
        os.utime(p, (old, old))
    assert purge_stale_tempfiles(root=str(tmp_path)) == 2
    assert not stale.exists() and not stale_dir.exists()
    assert fresh.exists() and foreign.exists()


def test_demarrage_reel_initialise_la_base(tmp_path, monkeypatch):
    """Lancement réel (événement startup) : la base est créée et les routes
    sociales répondent. Régression : un décorateur déplacé avait désactivé
    init_db() au démarrage, sans qu'aucun test (qui appellent init_db
    eux-mêmes) ne le voie."""
    from fastapi.testclient import TestClient
    from backend import db, main
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "fresh")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "fresh" / "live2mp3.db")
    monkeypatch.setattr(db, "AVATARS_DIR", tmp_path / "fresh" / "avatars")
    with TestClient(main.app) as c:                     # déclenche startup
        h = {"X-authentik-username": "alice", "X-authentik-groups": "live2mp3-user"}
        assert c.get("/api/social/me", headers=h).status_code == 200
        assert c.get("/api/social/counts").status_code == 200
