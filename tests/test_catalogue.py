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


def test_album_status_priorite():
    """Statut unique et prioritaire : brouillon > non publié > publié(mp3) > publié(av)."""
    S = catalogue.album_status
    # Non publié prime sur tout média présent.
    assert S(published=False, has_mp3=True, has_mp4=True, has_video_full=True) == "unpublished"
    assert S(published=False, has_mp3=True, has_mp4=False, has_video_full=False) == "unpublished"
    # Publié + vidéo (clips OU concert complet) -> audio+vidéo.
    assert S(published=True, has_mp3=True, has_mp4=True, has_video_full=False) == "published_av"
    assert S(published=True, has_mp3=True, has_mp4=False, has_video_full=True) == "published_av"
    # Publié, audio seul -> le MP4 manque.
    assert S(published=True, has_mp3=True, has_mp4=False, has_video_full=False) == "published_audio"


def test_labels_concert_complet_compte_comme_video():
    """Un concert complet (build/video-full) est de la vidéo comme les clips.

    Régression 2026-09-06 : `_labels` ignorait `has_video_full`, donc un album
    MP3 + concert complet affichait le libellé « audio » juste sous un badge
    « Publié (MP3 + MP4) » — deux affirmations contradictoires sur la même page.
    """
    L = catalogue._labels
    assert L({}, True, False, True)[0] == "audio + vidéo"
    assert L({}, False, False, True)[0] == "vidéo"
    # Sans concert complet, le comportement historique est inchangé.
    assert L({}, True, False, False)[0] == "audio"
    assert L({}, True, True, False)[0] == "audio + vidéo"
    # Les libellés manuels restent, le média passe en tête.
    assert L({"labels": ["concert"]}, True, False, True) == ["audio + vidéo", "concert"]


def test_backfill_ignore_le_mtime(synth_audio_only, monkeypatch):
    """Régression 2026-09-21 : `first_published_at` ne vient JAMAIS du mtime.

    Le mtime est réécrit par toute écriture technique (le backfill lui-même
    tournait à chaque démarrage) : des albums importés depuis des mois
    héritaient d'une date de mise en ligne récente et s'affichaient « Nouveau »
    puis « Mis à jour » par lots.
    """
    import os
    import time
    from backend.manifest import Manifest

    monkeypatch.setattr(catalogue, "PROJECTS_DIR", synth_audio_only.parent)
    path = synth_audio_only / "manifest.yaml"
    m = Manifest.load(path)
    m.data["published"] = True
    m.data["meta"] = {"imported_at": "2024-10-05T10:00:00+00:00"}
    m.save(path, touch=False)
    os.utime(path, (time.time(), time.time()))   # mtime = maintenant

    assert catalogue.backfill_first_published() == 1
    meta = Manifest.load(path).data["meta"]
    assert meta["first_published_at"] == "2024-10-05T10:00:00+00:00"
    assert meta["updated_at"] == "2024-10-05T10:00:00+00:00"

    # Passage unique : un second démarrage ne réécrit plus rien.
    assert catalogue.backfill_first_published() == 0


def test_backfill_sans_imported_at_n_invente_rien(synth_audio_only, monkeypatch):
    from backend.manifest import Manifest

    monkeypatch.setattr(catalogue, "PROJECTS_DIR", synth_audio_only.parent)
    path = synth_audio_only / "manifest.yaml"
    m = Manifest.load(path)
    m.data["published"] = True
    m.data["meta"] = {}
    m.save(path, touch=False)

    assert catalogue.backfill_first_published() == 0
    assert not Manifest.load(path).data["meta"].get("first_published_at")
