"""T4 — Artwork : PDF générés, dimensions exactes, fallback typo."""
import struct
from pathlib import Path

from backend.pipeline import artwork


def _pdf_page_size_mm(path: Path) -> tuple[float, float]:
    """Extrait la MediaBox (points) de la 1re page -> mm."""
    data = path.read_bytes()
    idx = data.find(b"/MediaBox")
    assert idx != -1, "MediaBox introuvable"
    seg = data[idx:idx + 80]
    start = seg.find(b"[")
    end = seg.find(b"]")
    nums = seg[start + 1:end].split()
    x0, y0, x1, y1 = (float(n) for n in nums[:4])
    pt2mm = 25.4 / 72.0
    return round((x1 - x0) * pt2mm), round((y1 - y0) * pt2mm)


def test_front_insert_generated(synth_project):
    out = artwork.run(synth_project)
    front = Path(out["front_insert"])
    assert front.exists()
    assert front.read_bytes()[:4] == b"%PDF"


def test_front_insert_dimensions(synth_project):
    out = artwork.run(synth_project)
    w, h = _pdf_page_size_mm(Path(out["front_insert"]))
    assert (w, h) == (120, 120)


def test_tray_card_a4(synth_project):
    out = artwork.run(synth_project)
    w, h = _pdf_page_size_mm(Path(out["tray_card"]))
    assert (w, h) == (210, 297)


def test_fallback_typo_when_no_cover(synth_audio_only):
    # Pas de cover -> le fallback typographique doit produire un PDF valide
    out = artwork.run(synth_audio_only)
    front = Path(out["front_insert"])
    assert front.exists()
    assert front.read_bytes()[:4] == b"%PDF"


def test_artwork_state_done(synth_project):
    artwork.run(synth_project)
    from backend.manifest import Manifest
    m = Manifest.load(synth_project / "manifest.yaml")
    assert m.state("artwork") == "done"
