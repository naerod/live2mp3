"""Autorisation basée sur les en-têtes injectés par le forward-auth Authentik.

nginx protège les routes sensibles via `auth_request` vers l'outpost Authentik,
puis transmet à l'app les en-têtes `X-authentik-username` / `X-authentik-groups`
(valeurs issues de la sous-requête d'auth, donc non spoofables par le client).

Rôles (groupes Authentik) :
- `live2mp3-user`         → téléchargement
- `live2mp3-gestionnaire` → outil complet + IA
- superuser / `authentik Admins` → tout
"""
from __future__ import annotations

from fastapi import Header, HTTPException

GROUP_USER = "live2mp3-user"
GROUP_GESTIONNAIRE = "live2mp3-gestionnaire"
SUPERUSER_GROUPS = {"authentik Admins", "admin"}


def _groups(header: str | None) -> set[str]:
    if not header:
        return set()
    # L'outpost sépare les groupes par des barres verticales.
    return {g.strip() for g in header.split("|") if g.strip()}


def current_identity(
    x_authentik_username: str | None = Header(default=None),
    x_authentik_groups: str | None = Header(default=None),
) -> dict:
    return {"username": x_authentik_username, "groups": _groups(x_authentik_groups)}


def _is_super(groups: set[str]) -> bool:
    return bool(groups & SUPERUSER_GROUPS)


def require_user(
    x_authentik_username: str | None = Header(default=None),
    x_authentik_groups: str | None = Header(default=None),
) -> dict:
    groups = _groups(x_authentik_groups)
    if not x_authentik_username:
        raise HTTPException(401, "authentification requise")
    if _is_super(groups) or groups & {GROUP_USER, GROUP_GESTIONNAIRE}:
        return {"username": x_authentik_username, "groups": groups}
    raise HTTPException(403, "accès téléchargement requis (live2mp3-user)")


def require_gestionnaire(
    x_authentik_username: str | None = Header(default=None),
    x_authentik_groups: str | None = Header(default=None),
) -> dict:
    groups = _groups(x_authentik_groups)
    if not x_authentik_username:
        raise HTTPException(401, "authentification requise")
    if _is_super(groups) or GROUP_GESTIONNAIRE in groups:
        return {"username": x_authentik_username, "groups": groups}
    raise HTTPException(403, "accès outil requis (live2mp3-gestionnaire)")
