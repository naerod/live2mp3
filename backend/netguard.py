"""Récupération d'URL côté serveur sans SSRF.

Toute URL fournie par un client (miniature, pochette distante…) passe par
`safe_get` : schéma http(s) uniquement, nom résolu puis **chaque** adresse
vérifiée publique (pas de loopback, réseaux privés, link-local / métadonnées
cloud 169.254.169.254, CGNAT/Tailscale 100.64/10, IPv6 locales, multicast,
réservées), redirections suivies à la main et revérifiées à chaque saut,
taille de réponse bornée.

Limite connue : entre notre résolution et celle de `requests`, un DNS
malveillant pourrait changer de réponse (rebinding). La fenêtre est de
quelques millisecondes ; l'éliminer imposerait de se connecter à l'IP
vérifiée (SNI/Host à gérer à la main).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import requests

MAX_REDIRECTS = 5
MAX_BYTES = 20 * 1024 * 1024
_EXTRA_BLOCKED = [ipaddress.ip_network("100.64.0.0/10")]   # CGNAT / Tailscale


class UnsafeURL(ValueError):
    """URL refusée (schéma, hôte interne, trop de redirections, trop lourde)."""


def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        return False
    return not any(ip in n for n in _EXTRA_BLOCKED if n.version == ip.version)


def check_url(url: str) -> None:
    """Lève UnsafeURL si l'URL ne vise pas exclusivement des hôtes publics."""
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeURL("schéma http(s) et hôte requis")
    host = parts.hostname
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as e:
        raise UnsafeURL(f"hôte introuvable : {host}") from e
    if not infos:
        raise UnsafeURL(f"hôte introuvable : {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not _ip_is_public(ip):
            raise UnsafeURL(f"hôte interne refusé : {host}")


def safe_get(url: str, timeout: float = 20, max_bytes: int = MAX_BYTES,
             **kwargs) -> requests.Response:
    """`requests.get` sûr : contrôle d'hôte à chaque saut de redirection."""
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url)
        r = requests.get(url, timeout=timeout, allow_redirects=False,
                         stream=True, **kwargs)
        if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location")
            r.close()
            if not loc:
                raise UnsafeURL("redirection sans destination")
            url = urljoin(url, loc)
            continue
        body = bytearray()
        for chunk in r.iter_content(64 * 1024):
            body += chunk
            if len(body) > max_bytes:
                r.close()
                raise UnsafeURL("réponse trop volumineuse")
        r._content = bytes(body)
        r._content_consumed = True
        return r
    raise UnsafeURL("trop de redirections")
