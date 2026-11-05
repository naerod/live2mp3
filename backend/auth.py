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


def is_gestionnaire_identity(identity: dict | None) -> bool:
    """Vrai pour gestionnaire / admin applicatif / superuser, quelle que soit la
    dépendance d'origine (`roles` pose `is_gestionnaire`, `require_*` et
    `current_identity` ne posent que `groups`). Point unique de vérité : les
    copies locales oubliaient l'admin applicatif (`live2mp3-admin`)."""
    identity = identity or {}
    if identity.get("is_gestionnaire"):
        return True
    return _is_gestionnaire(set(identity.get("groups") or ()))


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


# --- Garde-fou : en-têtes d'identité uniquement depuis le proxy ------------
# Les en-têtes X-authentik-* ne sont fiables que s'ils traversent nginx. Si le
# conteneur est joignable sans lui (port publié sur le LAN, réseau Docker
# partagé), n'importe qui peut se déclarer live2mp3-admin. Deux garde-fous
# opt-in (aucun changement tant qu'ils ne sont pas posés dans l'environnement),
# même contrat que showme :
#   - L2M_TRUSTED_PROXIES : IP/CIDR (virgules) autorisés à transmettre l'identité
#   - L2M_PROXY_SECRET    : secret attendu dans X-L2M-Proxy-Secret
#                           (nginx : proxy_set_header), comparé à temps constant
# Dans tous les cas, un username vide ou de plus de 150 caractères est ignoré.
import hmac as _hmac
import ipaddress as _ipaddress
import os as _os

IDENTITY_HEADERS = (b"x-authentik-username", b"x-authentik-groups",
                    b"x-authentik-email", b"x-authentik-name", b"x-authentik-uid")
PROXY_SECRET_HEADER = b"x-l2m-proxy-secret"
USERNAME_MAX = 150


def _trusted_nets() -> list:
    nets = []
    for part in _os.environ.get("L2M_TRUSTED_PROXIES", "").split(","):
        part = part.strip()
        if part:
            try:
                nets.append(_ipaddress.ip_network(part, strict=False))
            except ValueError:
                continue
    return nets


def identity_headers_trusted(client_host: str | None, headers: dict[bytes, bytes]) -> bool:
    """Les en-têtes d'identité viennent-ils bien du proxy d'authentification ?"""
    nets = _trusted_nets()
    if nets:
        try:
            ip = _ipaddress.ip_address(client_host or "")
        except ValueError:
            return False
        if not any(ip in n for n in nets):
            return False
    secret = _os.environ.get("L2M_PROXY_SECRET", "")
    if secret:
        given = headers.get(PROXY_SECRET_HEADER, b"")
        if not _hmac.compare_digest(given, secret.encode()):
            return False
    user = headers.get(b"x-authentik-username", b"").strip()
    if user and len(user) > USERNAME_MAX:
        return False
    return True


class ProxyHeaderGuard:
    """Middleware ASGI : retire les en-têtes d'identité non fiables **avant**
    toute route, y compris celles qui lisent Header() directement (logout).
    Le secret de proxy n'est jamais transmis à l'application."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            raw = scope.get("headers") or []
            headers = {k.lower(): v for k, v in raw}
            client = scope.get("client")
            trusted = identity_headers_trusted(client[0] if client else None, headers)
            drop = {PROXY_SECRET_HEADER} | (set() if trusted else set(IDENTITY_HEADERS))
            scope = dict(scope)
            scope["headers"] = [(k, v) for k, v in raw if k.lower() not in drop]
        await self.app(scope, receive, send)
