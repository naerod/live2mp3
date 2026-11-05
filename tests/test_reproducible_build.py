"""Build reproductible : images, audiowaveform et dépendances Python épinglés."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_images_docker_epinglees_par_digest():
    froms = re.findall(r"^FROM\s+(\S+)", (ROOT / "Dockerfile").read_text(), re.M)
    assert froms
    for img in froms:
        # deno : image « bin-x.y.z » versionnée ; les autres : tag + digest.
        assert "@sha256:" in img or re.search(r":bin-\d+\.\d+\.\d+$", img), img
        assert not img.endswith(":latest")


def test_audiowaveform_jamais_sur_head():
    df = (ROOT / "Dockerfile").read_text()
    assert "--branch \"${AUDIOWAVEFORM_TAG}\"" in df
    assert re.search(r"AUDIOWAVEFORM_COMMIT=[0-9a-f]{40}", df)


def test_requirements_tous_epingles():
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            assert re.match(r"^[A-Za-z0-9_.\-\[\]]+==[0-9][\w.]*$", line), line
