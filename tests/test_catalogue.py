"""Catalogue : labels (manuels + dérivés) et recherche côté données."""
from backend import catalogue
from backend.pipeline import render


def _album(project_dir, labels):
    from backend.manifest import Manifest
    m = Manifest.load(project_dir / "manifest.yaml")
    m.data["album"]["labels"] = labels
    m.save()


def test_labels_manual_plus_derived(synth_audio_only, monkeypatch, tmp_path):
    monkeypatch.setattr(catalogue, "PROJECTS_DIR", synth_audio_only.parent)
    _album(synth_audio_only, ["concert", "festival"])
    render.run(synth_audio_only, video=False)   # produit des mp3 -> label "audio"
    albums = catalogue.list_albums()
    assert len(albums) == 1
    labels = albums[0]["labels"]
    assert "audio" in labels
    assert "concert" in labels and "festival" in labels
    # "audio" en tête (dérivé de la disponibilité)
    assert labels[0] == "audio"


def test_all_labels_union(synth_audio_only, monkeypatch):
    monkeypatch.setattr(catalogue, "PROJECTS_DIR", synth_audio_only.parent)
    _album(synth_audio_only, ["high quality"])
    render.run(synth_audio_only, video=False)
    alll = catalogue.all_labels()
    assert "high quality" in alll and "audio" in alll


def test_no_media_no_album(synth_audio_only, monkeypatch):
    # sans rendu (pas de mp3/mp4), l'album n'apparaît pas dans le catalogue
    monkeypatch.setattr(catalogue, "PROJECTS_DIR", synth_audio_only.parent)
    assert catalogue.list_albums() == []
