"""Stage 8 — Bundle.

ZIP téléchargeable : build/audio, build/video, artwork/*.pdf, image disque,
et manifest.yaml.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from ..manifest import Manifest


def run(project_dir: str | Path, out_zip: str | Path | None = None) -> dict:
    project_dir = Path(project_dir)
    m = Manifest.load(project_dir / "manifest.yaml")
    out = Path(out_zip) if out_zip else project_dir / "build" / "bundle.zip"
    out.parent.mkdir(parents=True, exist_ok=True)

    included: list[str] = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        # manifest
        mpath = project_dir / "manifest.yaml"
        z.write(mpath, "manifest.yaml")
        included.append("manifest.yaml")

        for sub in ("build/audio", "build/video", "artwork", "build/disc"):
            base = project_dir / sub
            if not base.exists():
                continue
            for f in sorted(base.rglob("*")):
                if f.is_file() and f.suffix.lower() in {
                        ".mp3", ".mp4", ".pdf", ".iso", ".wav", ".cue"}:
                    arc = str(f.relative_to(project_dir))
                    z.write(f, arc)
                    included.append(arc)

    return {"bundle": str(out), "files": included, "count": len(included)}


if __name__ == "__main__":
    import sys

    run(sys.argv[1])
