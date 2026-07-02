"""T7 — data_disc : ISO générée, montable/lisible, contenu conforme."""
import subprocess
from pathlib import Path

from backend.pipeline import disc, render


def test_iso_generated(synth_project):
    render.run(synth_project, video=False)
    out = disc.run(synth_project, media="audio")
    iso = Path(out["iso"])
    assert iso.exists()
    assert iso.stat().st_size > 0


def test_iso_contains_tracks(synth_project):
    render.run(synth_project, video=False)
    out = disc.run(synth_project, media="audio")
    # isoinfo (fourni par genisoimage) liste le contenu
    listing = subprocess.run(
        ["isoinfo", "-f", "-i", out["iso"]],
        check=True, capture_output=True, text=True,
    ).stdout
    mp3_count = sum(1 for line in listing.splitlines()
                    if line.upper().endswith(".MP3") or ".MP3" in line.upper())
    assert mp3_count >= 4


def test_disc_state_done(synth_project):
    render.run(synth_project, video=False)
    disc.run(synth_project, media="audio")
    from backend.manifest import Manifest
    m = Manifest.load(synth_project / "manifest.yaml")
    assert m.state("disc") == "done"
