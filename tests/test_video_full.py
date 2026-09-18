"""Emplacement du MP4 concert complet — `build/video-full`.

C'est le seul dossier que la synchronisation Jellyfin de CT110 expose. Du
2026-08-13 au 2026-09-18, le rendu écrivait dans `build/video/` : les concerts
produits sur cette période n'apparaissaient jamais dans Jellyfin, alors que
leur MP4 était complet et valide. Ces tests verrouillent les trois propriétés
qui ont manqué : le bon dossier, la récupération sans réencodage, et la
survie des images sidecar.
"""
from __future__ import annotations

from backend.manifest import Manifest, jellyfin_stem
from backend.pipeline import render


def test_render_writes_to_video_full(synth_project):
    result = render.run(synth_project, video=True)
    mp4s = list((synth_project / "build" / "video-full").glob("*.mp4"))
    assert len(mp4s) == 1
    assert result["video"] == [str(mp4s[0])]
    # L'ancien dossier ne doit plus être alimenté du tout.
    assert not list((synth_project / "build" / "video").glob("*.mp4"))


def test_jellyfin_stem_is_human_readable():
    data = {"album": {"artist": "Linkin Park", "title": "Live at Corona Capital",
                      "date": "2025-11-16"}}
    assert jellyfin_stem(data, "slug") == "Linkin Park - Live at Corona Capital (2025-11-16)"
    # Champs manquants : omis, jamais remplacés par un marqueur.
    assert jellyfin_stem({"album": {"title": "Olympia"}}, "slug") == "Olympia"
    assert jellyfin_stem({"album": {"artist": "Gazo"}}, "slug") == "Gazo - Concert Complet"
    # Caractères interdits par le système de fichiers : retirés.
    assert "/" not in jellyfin_stem({"album": {"title": "AC/DC"}}, "slug")


def test_adopts_mp4_left_in_legacy_dir_without_reencoding(synth_project):
    """Le cas réel des albums rendus pendant la régression : le MP4 est dans
    `build/video`. Il doit rejoindre `build/video-full` **sans** repasser par
    ffmpeg — un réencodage, c'est plusieurs Go et 20 min pour rien."""
    render.run(synth_project, video=True)
    full_dir = synth_project / "build" / "video-full"
    legacy = synth_project / "build" / "video"
    legacy.mkdir(parents=True, exist_ok=True)
    mp4 = next(full_dir.glob("*.mp4"))
    orphan = legacy / "2024-09-27_tif_olympia_concert-complet.mp4"
    mp4.rename(orphan)
    ino, mtime = orphan.stat().st_ino, orphan.stat().st_mtime

    render.run(synth_project, video=True)

    moved = next(full_dir.glob("*.mp4"))
    m = Manifest.load(synth_project / "manifest.yaml")
    assert moved.name == render.video_filename(m, synth_project.name)
    # Même inode et même mtime = le fichier a été déplacé, pas réencodé.
    assert (moved.stat().st_ino, moved.stat().st_mtime) == (ino, mtime)
    assert not list(legacy.glob("*.mp4"))


def test_adoption_ignores_partial_render(synth_project):
    """Un `.part` est un encodage interrompu : l'adopter servirait une vidéo
    tronquée comme concert complet (cf. le rendu avorté du 2026-09-18)."""
    render.run(synth_project, video=True)
    full_dir = synth_project / "build" / "video-full"
    legacy = synth_project / "build" / "video"
    legacy.mkdir(parents=True, exist_ok=True)
    next(full_dir.glob("*.mp4")).rename(legacy / "concert.mp4.part")

    assert render.adopt_existing_video(synth_project) is None
    assert (legacy / "concert.mp4.part").exists()


def test_rename_on_metadata_change_keeps_file_and_sidecars(synth_project):
    """Corriger le titre renomme le MP4 et ses images sidecar Jellyfin, sans
    réencoder et sans laisser d'orphelin."""
    render.run(synth_project, video=True)
    full_dir = synth_project / "build" / "video-full"
    mp4 = next(full_dir.glob("*.mp4"))
    thumb = full_dir / f"{mp4.stem}-thumb.jpg"
    thumb.write_bytes(b"jpeg")
    ino = mp4.stat().st_ino

    m = Manifest.load(synth_project / "manifest.yaml")
    m.data["album"]["title"] = "Nouveau Titre"
    m.save()
    render.run(synth_project, video=True)

    mp4s = list(full_dir.glob("*.mp4"))
    assert len(mp4s) == 1
    assert mp4s[0].stat().st_ino == ino
    assert (full_dir / f"{mp4s[0].stem}-thumb.jpg").read_bytes() == b"jpeg"
    assert not thumb.exists()


def test_purge_preserves_jellyfin_sidecars(synth_project):
    """La purge des orphelins ne vise que les médias : les visuels posés par
    `covers.py` doivent survivre à un re-rendu, sinon Jellyfin perd sa
    miniature à chaque republication."""
    render.run(synth_project, video=True)
    full_dir = synth_project / "build" / "video-full"
    mp4 = next(full_dir.glob("*.mp4"))
    for suffix in ("-poster.jpg", "-thumb.jpg", "-banner.png"):
        (full_dir / f"{mp4.stem}{suffix}").write_bytes(b"img")

    render.run(synth_project, video=True, force=True)

    for suffix in ("-poster.jpg", "-thumb.jpg", "-banner.png"):
        assert (full_dir / f"{mp4.stem}{suffix}").exists()
