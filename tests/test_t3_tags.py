"""T3 — Tags : ID3 complets + APIC présents (relecture mutagen)."""
from pathlib import Path

from mutagen.id3 import ID3
from mutagen.mp4 import MP4

from backend.pipeline import render, tags


def test_id3_tags_complete(synth_project):
    render.run(synth_project, video=False)
    tags.run(synth_project)
    mp3s = sorted((synth_project / "build" / "audio").glob("*.mp3"))
    assert mp3s
    id3 = ID3(str(mp3s[0]))
    # Le tag titre est préfixé du numéro (« 01. » pour la piste 1) — c'est ce
    # qui force l'ordre des pistes sur les lecteurs qui trient par titre.
    assert id3["TIT2"].text[0] == "01. Overcompensate"
    assert id3["TPE1"].text[0] == "Twenty One Pilots"
    assert id3["TALB"].text[0].startswith("The Clancy Tour")
    assert id3["TCON"].text[0] == "Live"
    assert id3["TRCK"].text[0] == "1/4"
    assert str(id3["TDRC"].text[0]) == "2026"


def test_apic_cover_embedded(synth_project):
    render.run(synth_project, video=False)
    tags.run(synth_project)
    mp3 = sorted((synth_project / "build" / "audio").glob("*.mp3"))[0]
    id3 = ID3(str(mp3))
    apics = id3.getall("APIC")
    assert len(apics) == 1
    assert apics[0].data  # bytes présents
    assert apics[0].mime == "image/png"


def test_mp4_metadata(synth_project):
    """Le MP4 complet (un seul fichier pour tout le concert) porte les
    métadonnées de l'ALBUM, pas d'une piste — il n'y a plus de notion de
    piste au niveau du fichier vidéo depuis le 2026-08-02."""
    render.run(synth_project, video=True)
    tags.run(synth_project)
    mp4s = sorted((synth_project / "build" / "video").glob("*.mp4"))
    assert len(mp4s) == 1
    meta = MP4(str(mp4s[0]))
    assert meta.tags["\xa9nam"][0].startswith("The Clancy Tour")
    assert meta.tags["\xa9ART"][0] == "Twenty One Pilots"
    assert meta.tags["\xa9gen"][0] == "Live"


def test_tags_no_cover_fallback(synth_audio_only):
    # Pas de cover -> pas d'APIC, pas d'erreur
    render.run(synth_audio_only, video=False)
    tags.run(synth_audio_only)
    mp3 = sorted((synth_audio_only / "build" / "audio").glob("*.mp3"))[0]
    id3 = ID3(str(mp3))
    assert id3.getall("APIC") == []
