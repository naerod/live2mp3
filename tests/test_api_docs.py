"""Documentation d'API : jamais publique en prod."""
import importlib
import sys

import pytest
from fastapi.testclient import TestClient


def _fresh_app(monkeypatch, **env):
    for k in ("APP_ENV", "L2M_API_DOCS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("backend.main", None)
    main = importlib.import_module("backend.main")
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _restore():
    yield
    sys.modules.pop("backend.main", None)
    importlib.import_module("backend.main")


def test_docs_coupees_en_prod(monkeypatch):
    c = _fresh_app(monkeypatch, APP_ENV="prod")
    for url in ("/api/docs", "/redoc", "/openapi.json", "/api/openapi.json", "/docs"):
        assert c.get(url).status_code == 404, url


def test_docs_disponibles_en_preprod(monkeypatch):
    c = _fresh_app(monkeypatch, APP_ENV="preprod")
    assert c.get("/api/docs").status_code == 200
    assert c.get("/api/openapi.json").status_code == 200
    assert c.get("/redoc").status_code == 404


def test_docs_reactivables_explicitement(monkeypatch):
    c = _fresh_app(monkeypatch, APP_ENV="prod", L2M_API_DOCS="1")
    assert c.get("/api/docs").status_code == 200
