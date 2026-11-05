"""SSRF : toute URL récupérée côté serveur ne vise que des hôtes publics."""
import socket

import pytest

from backend import netguard
from backend.netguard import UnsafeURL, check_url, safe_get


def _resolve_to(monkeypatch, mapping):
    def fake(host, port, *a, **kw):
        ip = mapping[host]
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(netguard.socket, "getaddrinfo", fake)


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "127.8.9.10", "10.1.2.3", "172.16.0.1", "172.31.255.254",
    "192.168.1.110", "169.254.169.254", "100.100.1.1", "0.0.0.0",
    "::1", "fe80::1", "fc00::1", "fd12:3456::1", "::ffff:127.0.0.1", "224.0.0.1",
])
def test_hotes_internes_refuses(monkeypatch, ip):
    _resolve_to(monkeypatch, {"evil.example": ip})
    with pytest.raises(UnsafeURL):
        check_url("http://evil.example/x.jpg")


def test_ip_litterale_interne_refusee():
    for url in ("http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/latest/meta-data/"):
        with pytest.raises(UnsafeURL):
            check_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://x/", "//x/", ""])
def test_schemas_refuses(url):
    with pytest.raises(UnsafeURL):
        check_url(url)


def test_hote_public_accepte(monkeypatch):
    _resolve_to(monkeypatch, {"i.ytimg.com": "142.250.74.246"})
    check_url("https://i.ytimg.com/vi/x/hq.jpg")


def test_une_seule_ip_interne_suffit_a_refuser(monkeypatch):
    def fake(host, port, *a, **kw):
        return [(socket.AF_INET, 1, 6, "", ("142.250.74.246", port)),
                (socket.AF_INET, 1, 6, "", ("10.0.0.5", port))]
    monkeypatch.setattr(netguard.socket, "getaddrinfo", fake)
    with pytest.raises(UnsafeURL):
        check_url("http://mixed.example/")


class _Resp:
    def __init__(self, status, headers=None, body=b""):
        self.status_code, self.headers, self._body = status, headers or {}, body
        self.is_redirect = status in (301, 302, 303, 307, 308)

    def iter_content(self, n):
        yield self._body

    def close(self):
        pass


def test_redirection_vers_interne_refusee(monkeypatch):
    _resolve_to(monkeypatch, {"cdn.example": "93.184.216.34", "internal": "10.0.0.1"})
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        assert kw["allow_redirects"] is False
        return _Resp(302, {"Location": "http://internal/admin"})
    monkeypatch.setattr(netguard.requests, "get", fake_get)
    with pytest.raises(UnsafeURL):
        safe_get("http://cdn.example/a.jpg")
    assert calls == ["http://cdn.example/a.jpg"]   # la cible interne n'est jamais contactée


def test_redirection_publique_suivie_et_taille_bornee(monkeypatch):
    _resolve_to(monkeypatch, {"a.example": "93.184.216.34", "b.example": "93.184.216.35"})
    seq = iter([_Resp(301, {"Location": "https://b.example/img"}), _Resp(200, body=b"IMG")])
    monkeypatch.setattr(netguard.requests, "get", lambda url, **kw: next(seq))
    assert safe_get("http://a.example/")._content == b"IMG"
    seq2 = iter([_Resp(200, body=b"x" * 100)])
    monkeypatch.setattr(netguard.requests, "get", lambda url, **kw: next(seq2))
    with pytest.raises(UnsafeURL):
        safe_get("http://a.example/", max_bytes=10)


def test_trop_de_redirections(monkeypatch):
    _resolve_to(monkeypatch, {"a.example": "93.184.216.34"})
    monkeypatch.setattr(netguard.requests, "get",
                        lambda url, **kw: _Resp(302, {"Location": "/again"}))
    with pytest.raises(UnsafeURL):
        safe_get("http://a.example/")


def test_miniature_interne_ignoree_par_register_thumbnail(monkeypatch, tmp_path):
    """Bout en bout : la miniature d'un job pointant vers l'intranet n'est
    jamais téléchargée."""
    from backend import linktool
    hit = []
    monkeypatch.setattr(netguard.requests, "get", lambda *a, **kw: hit.append(a) or _Resp(200))
    with pytest.raises(UnsafeURL):
        linktool.safe_get("http://127.0.0.1:8000/api/me")
    assert hit == []
