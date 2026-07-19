"""T6 — DeepSeek : parsing JSON, timecodes plausibles, respect `locked`.

Le parsing et la règle anti-écrasement sont testés en mock (déterministe).
Un appel réel à l'API DeepSeek est tenté si la clé est disponible ; il est
skippé proprement en cas d'absence de clé ou d'erreur réseau (offline CI).
"""
import os
from pathlib import Path

import pytest

from backend import llm
from backend.manifest import new_manifest


def _load_env():
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def test_parse_plain_json():
    txt = '{"tracks": [{"n": 1, "start": 0.0, "end": 182.4}, {"n": 2, "start": 182.4, "end": 300.0}]}'
    markers = llm.parse_markers(txt)
    assert markers[1] == {"start": 0.0, "end": 182.4}
    assert markers[2]["end"] == 300.0


def test_parse_json_with_markdown_fence():
    txt = "```json\n{\"tracks\": [{\"n\": 1, \"start\": 1.5, \"end\": 9.0}]}\n```"
    markers = llm.parse_markers(txt)
    assert markers[1]["start"] == 1.5


def test_parse_json_with_prose_around():
    txt = "Voici le résultat: {\"tracks\": [{\"n\": 3, \"start\": 5, \"end\": 8}]} fin."
    markers = llm.parse_markers(txt)
    assert markers[3] == {"start": 5.0, "end": 8.0}


def test_build_user_prompt_contains_data():
    prompt = llm.build_user_prompt(
        [{"n": 1, "title": "Song A"}],
        [{"start": 0.0, "end": 3.0, "text": "hello"}],
        [{"time": 3.2, "depth": 11.5}],
    )
    assert "Song A" in prompt
    assert "hello" in prompt
    assert "SETLIST" in prompt
    # Les candidats de coupe (creux d'énergie) et leur profondeur sont transmis.
    assert "CANDIDATS" in prompt
    assert "t=3.2" in prompt and "depth=11.5" in prompt


def test_markers_respect_locked():
    m = new_manifest(
        {"artist": "X", "title": "Y"},
        [{"n": 1, "title": "A", "locked": True},
         {"n": 2, "title": "B", "locked": False}],
        target="data_disc",
    )
    markers = llm.parse_markers(
        '{"tracks":[{"n":1,"start":9,"end":9},{"n":2,"start":10,"end":20}]}')
    updated = m.merge_ai_markers(markers)
    assert updated == 1
    assert m.tracks[0]["start"] is None
    assert m.tracks[1]["start"] == 10.0


def test_real_deepseek_call():
    _load_env()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY absent — appel réel non testé")
    setlist = [{"n": 1, "title": "First"}, {"n": 2, "title": "Second"}]
    transcript = [
        {"start": 0.0, "end": 5.0, "text": "welcome this is the first song"},
        {"start": 30.0, "end": 35.0, "text": "thank you here is the second"},
    ]
    silences = [{"start": 28.0, "end": 30.0}]
    try:
        markers = llm.request_markers(setlist, transcript, silences, timeout=60)
    except Exception as e:  # réseau indisponible / API down
        pytest.skip(f"Appel DeepSeek indisponible: {e}")
    assert 1 in markers and 2 in markers
    assert markers[1]["end"] >= markers[1]["start"]
