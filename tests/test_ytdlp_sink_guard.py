"""Garde-fou au plus près de yt-dlp : aucune URL non http(s) ne doit atteindre
la commande externe, même si elle ne passe pas par une route validée
(MP4 extrait d'une page, manifest sur disque, appel interne)."""
import subprocess

import pytest

from backend import linktool
from backend.pipeline import download

BAD = ["--exec=touch /tmp/pwn", "-o/etc/x", "file:///etc/passwd",
       "ftp://host/x", "javascript:alert(1)", "http://a b", ""]


@pytest.fixture
def no_subprocess(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("commande externe lancée avec une URL invalide")
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    return calls


@pytest.mark.parametrize("url", BAD)
def test_download_master_refuse(url, tmp_path, no_subprocess):
    with pytest.raises(ValueError):
        download.download_master(url, tmp_path / "m.mkv")
    assert no_subprocess == []


@pytest.mark.parametrize("url", BAD)
def test_download_audio_refuse(url, tmp_path, no_subprocess):
    with pytest.raises(ValueError):
        download.download_audio(url, tmp_path)
    assert no_subprocess == []


@pytest.mark.parametrize("url", BAD)
def test_probe_refuse(url, no_subprocess):
    with pytest.raises(linktool.ProbeError):
        linktool.probe_url(url)
    assert no_subprocess == []


def test_url_valide_passe_apres_double_tiret(monkeypatch, tmp_path):
    seen = {}

    class P:
        stdout = iter(())
        returncode = 0

        def wait(self):
            pass

    def fake(cmd, **k):
        seen["cmd"] = cmd
        return P()
    monkeypatch.setattr(subprocess, "Popen", fake)
    download.download_master("HTTPS://youtu.be/x", tmp_path / "m.mkv")
    assert seen["cmd"][-2:] == ["--", "HTTPS://youtu.be/x"]
