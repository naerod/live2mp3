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
# Admin **applicatif** : gestionnaire + droit de gérer les rôles (promouvoir des
# utilisateurs, rétrograder des gestionnaires). N'est PAS superuser Authentik.
GROUP_APP_ADMIN = "live2mp3-admin"
# Superuser Authentik global (toute l'infra) — distinct de l'admin applicatif ;
# jamais attribué/retiré depuis le site.
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
    """Superuser Authentik global."""
    return bool(groups & SUPERUSER_GROUPS)


def _is_admin(groups: set[str]) -> bool:
    """Gestion des rôles : superuser global OU admin applicatif."""
    return _is_super(groups) or GROUP_APP_ADMIN in groups


def _is_gestionnaire(groups: set[str]) -> bool:
    return _is_admin(groups) or GROUP_GESTIONNAIRE in groups


def require_user(
    x_authentik_username: str | None = Header(default=None),
    x_authentik_groups: str | None = Header(default=None),
) -> dict:
    groups = _groups(x_authentik_groups)
    if not x_authentik_username:
        raise HTTPException(401, "authentification requise")
    if _is_gestionnaire(groups) or GROUP_USER in groups:
        return {"username": x_authentik_username, "groups": groups}
    raise HTTPException(403, "accès téléchargement requis (live2mp3-user)")


def require_gestionnaire(
    x_authentik_username: str | None = Header(default=None),
    x_authentik_groups: str | None = Header(default=None),
) -> dict:
    groups = _groups(x_authentik_groups)
    if not x_authentik_username:
        raise HTTPException(401, "authentification requise")
    if _is_gestionnaire(groups):
        return {"username": x_authentik_username, "groups": groups}
    raise HTTPException(403, "accès outil requis (live2mp3-gestionnaire)")


def require_admin(
    x_authentik_username: str | None = Header(default=None),
    x_authentik_groups: str | None = Header(default=None),
) -> dict:
    """Gestion des rôles : superuser global OU admin applicatif. L'autorisation
    se base sur les groupes *transmis en direct* par le forward-auth (jamais le
    rôle en cache), pour éviter toute élévation via un cache obsolète."""
    groups = _groups(x_authentik_groups)
    if not x_authentik_username:
        raise HTTPException(401, "authentification requise")
    if not _is_admin(groups):
        raise HTTPException(403, "réservé aux administrateurs")
    return {"username": x_authentik_username, "groups": groups}


def roles(
    x_authentik_username: str | None = Header(default=None),
    x_authentik_groups: str | None = Header(default=None),
) -> dict:
    """État de connexion et rôles (sans lever d'erreur) — pour /api/me."""
    groups = _groups(x_authentik_groups)
    is_super = _is_super(groups)
    is_admin = _is_admin(groups)
    is_gest = _is_gestionnaire(groups)
    is_user = is_gest or GROUP_USER in groups
    return {
        "authenticated": bool(x_authentik_username) and is_user,
        "username": x_authentik_username,
        "is_user": is_user,
        "is_gestionnaire": is_gest,
        "is_admin": is_admin,          # gestion des rôles (super ou admin appli)
        "is_superadmin": is_super,     # superuser Authentik global
    }
