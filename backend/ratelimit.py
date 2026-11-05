"""Limites anti-abus : débit par client et garde « disque plein ».

Middleware ASGI, en mémoire (un worker uvicorn : suffisant ; plusieurs
workers diviseraient la limite effective, cf. NOTES.md).

- Débit : fenêtre glissante de 60 s. Écritures (POST/PUT/PATCH/DELETE)
  `L2M_RATE_WRITE` (défaut 60/min), lectures `L2M_RATE_READ` (défaut
  600/min). Clé = utilisateur Authentik s'il est connu, sinon IP réelle
  (premier saut de X-Forwarded-For **uniquement** si la connexion vient d'un
  proxy de confiance, cf. L2M_TRUSTED_PROXIES). Dépassement → 429 +
  Retry-After. `L2M_RATE_WRITE=0` / `L2M_RATE_READ=0` désactive.
- Disque : toute écriture qui dépose des fichiers (import, jobs, pochettes,
  avatar, ajout de piste) est refusée en 507 quand l'espace libre du volume
  des projets passe sous `L2M_MIN_FREE_MB` (défaut 2048). À 100 %, le site
  reste donc lisible au lieu d'échouer au milieu d'un rendu.
"""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
import threading
import time
from collections import deque

from .auth import _trusted_nets
from .manifest import PROJECTS_DIR

WINDOW_S = 60.0
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Préfixes de routes qui déposent des fichiers sur le volume partagé.
_DISK_PREFIXES = ("/api/import", "/api/jobs", "/api/tool", "/api/albums",
                  "/api/social/profile/avatar")
_DISK_SUFFIXES = ("/covers", "/images")
_EXEMPT = ("/healthz", "/api/version", "/static/")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class _Window:
    def __init__(self) -> None:
        self.hits: dict[str, deque] = {}
        self.lock = threading.Lock()

    def hit(self, key: str, limit: int, now: float) -> float:
        """0 si accepté, sinon secondes avant la prochaine place libre."""
        with self.lock:
            q = self.hits.setdefault(key, deque())
            while q and q[0] <= now - WINDOW_S:
                q.popleft()
            if len(q) >= limit:
                return max(0.0, q[0] + WINDOW_S - now)
            q.append(now)
            if len(self.hits) > 50_000:          # borne mémoire
                for k in [k for k, v in self.hits.items() if not v][:10_000]:
                    self.hits.pop(k, None)
            return 0.0


def client_key(scope) -> str:
    headers = {k.lower(): v for k, v in scope.get("headers") or []}
    user = headers.get(b"x-authentik-username", b"").decode("latin-1").strip()
    if user:
        return "u:" + user
    host = (scope.get("client") or ("", 0))[0] or ""
    xff = headers.get(b"x-forwarded-for", b"").decode("latin-1")
    if xff:
        try:
            ip = ipaddress.ip_address(host)
            if any(ip in n for n in _trusted_nets()):
                host = xff.split(",")[0].strip() or host
        except ValueError:
            pass
    return "ip:" + host


def disk_low(path=None) -> bool:
    min_free = _int_env("L2M_MIN_FREE_MB", 2048) * 1024 * 1024
    target = path or PROJECTS_DIR
    try:
        target.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(target).free < min_free
    except OSError:
        return False


def _writes_files(method: str, path: str) -> bool:
    if method not in ("POST", "PUT"):
        return False
    return path.startswith(_DISK_PREFIXES) or path.endswith(_DISK_SUFFIXES)


class RateLimit:
    def __init__(self, app):
        self.app = app
        self.win = _Window()

    async def _reply(self, send, status: int, msg: str, retry: float | None = None):
        headers = [(b"content-type", b"application/json")]
        if retry is not None:
            headers.append((b"retry-after", str(max(1, int(retry + 0.999))).encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body",
                    "body": json.dumps({"detail": msg}).encode()})

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path, method = scope.get("path", ""), scope.get("method", "GET")
        if path.startswith(_EXEMPT):
            return await self.app(scope, receive, send)
        write = method in _WRITE_METHODS
        limit = _int_env("L2M_RATE_WRITE" if write else "L2M_RATE_READ",
                         60 if write else 600)
        if limit > 0:
            wait = self.win.hit(("w|" if write else "r|") + client_key(scope),
                                limit, time.monotonic())
            if wait:
                return await self._reply(send, 429, "trop de requêtes, réessayez plus tard", wait)
        if _writes_files(method, path) and disk_low():
            return await self._reply(send, 507, "espace disque insuffisant sur le serveur")
        await self.app(scope, receive, send)
