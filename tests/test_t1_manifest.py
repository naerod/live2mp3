"""T1 — Manifest : load/save/validation, slugify, règle anti-écrasement."""
import pytest

from backend.manifest import (
    Manifest,
    ManifestError,
    clean_filename,
    new_manifest,
    project_slug,
    slugify,
)


def test_slugify():
    assert slugify("Twenty One Pilots") == "twenty-one-pilots"
    assert slugify("Café · Déjà–Vu") == "cafe-deja-vu"
    assert slugify("") == "untitled"


def test_clean_filename_medley():
    # Séparateurs et caractères spéciaux nettoyés
    assert clean_filename("Shy Away / Heathens / Next Semester") == \
        "Shy_Away-Heathens-Next_Semester"
    assert "/" not in clean_filename("A/B")
    assert "·" not in clean_filename("Song · Live")


def test_project_slug():
    m = {"album": {"artist": "Twenty One Pilots", "date": "2026-04-03"}}
    assert project_slug(m) == "twenty-one-pilots-2026-04-03"


def test_new_manifest_valid():
    m = new_manifest(
        {"artist": "X", "title": "Y"},
        [{"title": "T1"}, {"title": "T2"}],
        target="data_disc",
    )
    assert len(m.tracks) == 2
    assert m.tracks[0]["n"] == 1
    assert m.tracks[0]["locked"] is False


def test_validation_bad_target():
    with pytest.raises(ManifestError):
        new_manifest({"artist": "X", "title": "Y"},
                     [{"title": "T"}], target="dvd_video")


def test_validation_requires_tracks():
    with pytest.raises(ManifestError):
        new_manifest({"artist": "X", "title": "Y"}, [], target="data_disc")


def test_validation_duplicate_track_n():
    with pytest.raises(ManifestError):
        new_manifest({"artist": "X", "title": "Y"},
                     [{"n": 1, "title": "A"}, {"n": 1, "title": "B"}],
                     target="data_disc")


def test_save_load_roundtrip(tmp_path):
    m = new_manifest({"artist": "X", "title": "Y"},
                     [{"title": "T"}], target="audio_cd")
    p = tmp_path / "manifest.yaml"
    m.save(p)
    m2 = Manifest.load(p)
    assert m2.data["album"]["artist"] == "X"
    assert m2.data["target"] == "audio_cd"


def test_anti_overwrite_rule():
    m = new_manifest(
        {"artist": "X", "title": "Y"},
        [{"n": 1, "title": "A", "locked": True},
         {"n": 2, "title": "B", "locked": False}],
        target="data_disc",
    )
    updated = m.merge_ai_markers({
        1: {"start": 10.0, "end": 20.0},
        2: {"start": 30.0, "end": 40.0},
    })
    assert updated == 1  # seule la piste 2 (unlocked) est modifiée
    assert m.tracks[0]["start"] is None  # piste 1 verrouillée intouchée
    assert m.tracks[1]["start"] == 30.0


def test_lock_all():
    m = new_manifest({"artist": "X", "title": "Y"},
                     [{"title": "A"}, {"title": "B"}], target="data_disc")
    m.lock_all()
    assert all(t["locked"] for t in m.tracks)
