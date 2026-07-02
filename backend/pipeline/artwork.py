"""Stage 6 — Visuels pochette.

Remplit les templates HTML (front insert + tray card) avec les variables du
manifest et les rend en PDF via Chromium headless.

Le pipeline gère uniquement la MISE EN PAGE ; l'artwork créatif (recto) est
fourni par l'utilisateur (`album.cover`). Si absent : fallback typographique.
"""
from __future__ import annotations

import base64
import mimetypes
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..manifest import Manifest

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"


def _chromium_bin() -> str:
    for cand in (os.environ.get("CHROMIUM_BIN"), "chromium", "chromium-browser",
                 "google-chrome"):
        if cand and shutil.which(cand):
            return shutil.which(cand)
    if os.environ.get("CHROMIUM_BIN"):
        return os.environ["CHROMIUM_BIN"]
    raise RuntimeError("Chromium introuvable (installer chromium ou définir CHROMIUM_BIN).")


def _cover_data_uri(project_dir: Path, manifest: Manifest) -> str | None:
    cover_rel = manifest.data.get("album", {}).get("cover")
    if not cover_rel:
        return None
    cover_path = project_dir / cover_rel
    if not cover_path.exists():
        return None
    mime = mimetypes.guess_type(str(cover_path))[0] or "image/png"
    b64 = base64.b64encode(cover_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _render_pdf(html: str, out_pdf: Path) -> None:
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(html)
        html_path = fh.name
    try:
        with tempfile.TemporaryDirectory() as profile:
            subprocess.run([
                _chromium_bin(), "--headless=new", "--no-sandbox",
                "--disable-gpu", f"--user-data-dir={profile}",
                "--no-pdf-header-footer", "--no-margins",
                f"--print-to-pdf={out_pdf}", f"file://{html_path}",
            ], check=True, capture_output=True, timeout=120)
    finally:
        os.unlink(html_path)


def run(project_dir: str | Path) -> dict:
    project_dir = Path(project_dir)
    m = Manifest.load(project_dir / "manifest.yaml")
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    ctx = {
        "album": m.data.get("album", {}),
        "tracks": m.tracks,
        "track_count": len(m.tracks),
        "cover_data_uri": _cover_data_uri(project_dir, m),
    }
    artwork_dir = project_dir / "artwork"
    outputs = {}

    front_html = env.get_template("front_insert.html").render(**ctx)
    front_pdf = artwork_dir / "front_insert.pdf"
    _render_pdf(front_html, front_pdf)
    outputs["front_insert"] = str(front_pdf)

    tray_html = env.get_template("tray_card.html").render(**ctx)
    tray_pdf = artwork_dir / "tray_card.pdf"
    _render_pdf(tray_html, tray_pdf)
    outputs["tray_card"] = str(tray_pdf)

    m.set_state("artwork", "done")
    return outputs


if __name__ == "__main__":
    import sys

    run(sys.argv[1])
