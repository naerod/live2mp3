"""Ré-applique les tags ID3 de tous les albums existants.

Sert à propager un changement de convention de tags (ici : titres préfixés du
numéro de piste, « 01. Titre ») sur les MP3 déjà rendus. Idempotent : peut être
rejoué sans risque.

Usage (dans le conteneur) :  python -m backend.retag_all
"""
from __future__ import annotations

from .albumfiles import _write_album_tags, _write_track_tags
from .manifest import PROJECTS_DIR, Manifest


def main() -> None:
    albums = 0
    tracks = 0
    for pdir in sorted(PROJECTS_DIR.iterdir()):
        mf = pdir / "manifest.yaml"
        if not mf.exists():
            continue
        try:
            m = Manifest.load(mf)
        except Exception as exc:  # album illisible : on continue
            print(f"! {pdir.name}: manifeste illisible ({exc})")
            continue
        n = _write_track_tags(pdir.name, m)
        _write_album_tags(pdir.name, m)
        if n:
            albums += 1
            tracks += n
        print(f"  {pdir.name}: {n} piste(s) retaguée(s)")
    print(f"TOTAL : {albums} album(s), {tracks} piste(s) retaguée(s)")


if __name__ == "__main__":
    main()
