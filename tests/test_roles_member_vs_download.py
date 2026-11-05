"""« Connecté » (tout compte Authentik) ≠ « téléchargement » (live2mp3-user).

Un nouvel inscrit (aucun groupe live2mp3) doit pouvoir aimer, commenter,
suivre et gérer son profil ; le téléchargement lui reste refusé (403).
Chaque route est testée avec les trois rôles : anonyme, connecté, live2mp3-user.
"""
import pytest

from tests.test_audit_security import GEST, _make_album, client  # noqa: F401

ANON: dict = {}
MEMBER = {"X-authentik-username": "newbie", "X-authentik-groups": ""}
DL = {"X-authentik-username": "alice", "X-authentik-groups": "live2mp3-user"}

# (méthode, chemin, json) — `{slug}` remplacé par un album publié.
MEMBER_ROUTES = [
    ("put", "/api/social/profile", {"display_name": "N", "bio": ""}),
    ("delete", "/api/social/profile/avatar", None),
    ("post", "/api/social/albums/{slug}/like", None),
    ("post", "/api/social/albums/{slug}/comments", {"body": "super"}),
    ("post", "/api/social/follow", {"target_type": "user", "target_id": "mod"}),
    ("get", "/api/social/notif-prefs", None),
    ("post", "/api/social/notifications/read", {}),
]
DOWNLOAD_ROUTES = [
    ("get", "/download/{slug}/video", None),
    ("get", "/download/{slug}/artwork", None),
    ("get", "/download/{slug}/cover", None),
    ("get", "/download/{slug}/track/1", None),
    ("get", "/download/{slug}/traycard", None),
]


def _call(c, method, path, body, headers, slug):
    kw = {"headers": headers}
    if body is not None:
        kw["json"] = body
    return getattr(c, method)(path.format(slug=slug), **kw)


@pytest.mark.parametrize("method,path,body", MEMBER_ROUTES)
def test_routes_connecte(client, method, path, body):  # noqa: F811
    c, _ = client
    slug = _make_album(c)
    assert _call(c, method, path, body, ANON, slug).status_code == 401
    for who in (MEMBER, DL):
        r = _call(c, method, path, body, who, slug)
        assert r.status_code not in (401, 403), (who, r.status_code, r.text)


@pytest.mark.parametrize("method,path,body", DOWNLOAD_ROUTES)
def test_routes_telechargement(client, method, path, body):  # noqa: F811
    c, _ = client
    slug = _make_album(c)
    assert _call(c, method, path, body, ANON, slug).status_code == 401
    assert _call(c, method, path, body, MEMBER, slug).status_code == 403
    r = _call(c, method, path, body, DL, slug)
    assert r.status_code not in (401, 403), r.text  # 404 : pas de fichier rendu


def test_me_distingue_connecte_et_telechargement(client):  # noqa: F811
    c, _ = client
    for url in ("/api/me", "/api/social/me"):
        assert c.get(url).json()["authenticated"] is False
        m = c.get(url, headers=MEMBER).json()
        assert m["authenticated"] is True and m["can_download"] is False
        u = c.get(url, headers=DL).json()
        assert u["authenticated"] is True and u["can_download"] is True
