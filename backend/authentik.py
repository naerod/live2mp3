"""Client minimal de l'API Authentik — gestion de l'appartenance à un groupe.

Sert la promotion/rétrogradation « gestionnaire » depuis le site : l'admin
ajoute ou retire l'utilisateur du groupe applicatif `live2mp3-gestionnaire`
directement dans Authentik (source de vérité des rôles).

Configuration par variables d'environnement (injectées dans le conteneur) :
- `AUTHENTIK_URL` : base de l'instance (défaut réseau Docker `authentik-server:9000`) ;
- `AUTHENTIK_API_TOKEN` : token d'API (créé côté Authentik, `claude-api-token`) ;
- `AUTHENTIK_GESTIONNAIRE_GROUP` : nom du groupe (défaut `live2mp3-gestionnaire`).

Sans token, `enabled()` est faux → l'appelant renvoie une erreur claire au lieu
d'échouer sur un 403.
"""
from __future__ import annotations

import os

import requests

AK_URL = os.environ.get("AUTHENTIK_URL", "http://authentik-server:9000").rstrip("/")
AK_TOKEN = os.environ.get("AUTHENTIK_API_TOKEN", "").strip()
GEST_GROUP = os.environ.get("AUTHENTIK_GESTIONNAIRE_GROUP", "live2mp3-gestionnaire")

_TIMEOUT = 8
_group_pk_cache: dict[str, str] = {}


class AuthentikError(Exception):
    """Échec d'un appel à l'API Authentik (indisponible, 4xx/5xx, absence)."""


def enabled() -> bool:
    return bool(AK_TOKEN)


def _api(method: str, path: str, **kw) -> requests.Response:
    if not AK_TOKEN:
        raise AuthentikError("token Authentik absent")
    try:
        r = requests.request(
            method, f"{AK_URL}/api/v3{path}",
            headers={"Authorization": f"Bearer {AK_TOKEN}"},
            timeout=_TIMEOUT, **kw,
        )
    except requests.RequestException as e:
        raise AuthentikError(f"Authentik injoignable: {e}") from e
    if r.status_code >= 400:
        raise AuthentikError(f"{method} {path} -> {r.status_code}")
    return r


def _group_pk(name: str) -> str:
    if name in _group_pk_cache:
        return _group_pk_cache[name]
    results = _api("GET", "/core/groups/", params={"name": name}).json().get("results", [])
    for g in results:
        if g.get("name") == name:
            _group_pk_cache[name] = g["pk"]
            return g["pk"]
    raise AuthentikError(f"groupe {name!r} introuvable")


def _user(username: str) -> dict:
    results = _api("GET", "/core/users/", params={"username": username}).json().get("results", [])
    for u in results:
        if u.get("username") == username:
            return u
    raise AuthentikError(f"utilisateur {username!r} introuvable")


def user_groups(username: str) -> set[str]:
    """Noms des groupes Authentik d'un utilisateur (source de vérité du rôle)."""
    return {g["name"] for g in _user(username).get("groups_obj", [])}


def set_gestionnaire(username: str, grant: bool) -> set[str]:
    """Ajoute (`grant=True`) ou retire l'utilisateur du groupe gestionnaire.

    Retourne l'ensemble des groupes résultant, pour recalculer le rôle. Les
    endpoints `add_user`/`remove_user` sont idempotents (204 même si l'état ne
    change pas)."""
    gpk = _group_pk(GEST_GROUP)
    user = _user(username)
    action = "add_user" if grant else "remove_user"
    _api("POST", f"/core/groups/{gpk}/{action}/", json={"pk": user["pk"]})
    return user_groups(username)
