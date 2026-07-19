"""Backfill des liens canoniques sur les albums existants (à lancer une fois).

Pour chaque manifest :
- `festival_id` dérivé du libellé festival s'il manque (aucun réseau) ;
- `artist_id` résolu via Deezer si le nom correspond exactement à la 1re
  suggestion (casefold). En cas de doute, on ne touche à rien — un gestionnaire
  choisira dans la liste depuis l'éditeur d'album.

Usage :
    python -m backend.backfill_entities [--apply] [--artists]

Sans `--apply` : dry-run (n'écrit rien, affiche ce qui serait fait).
Sans `--artists` : ne fait que les `festival_id` (pas d'appel réseau).
Les manifests étant partagés prod/preprod, un seul passage suffit.
"""
from __future__ import annotations

import argparse
import sys

from . import entities, suggest
from .manifest import Manifest, PROJECTS_DIR


def _resolve_artist_id(name: str) -> tuple[str, str] | None:
    """(id, label) si la 1re suggestion Deezer correspond exactement au nom."""
    name = (name or "").strip()
    if not name:
        return None
    try:
        results = suggest.search_artists(name)
    except Exception:
        return None
    for r in results:
        if r["label"].casefold() == name.casefold():
            return r["id"], r["label"]
    return None


def run(apply: bool = False, artists: bool = False) -> dict:
    stats = {"scanned": 0, "festival_id": 0, "artist_id": 0, "artist_skipped": 0}
    if not PROJECTS_DIR.exists():
        print(f"PROJECTS_DIR introuvable: {PROJECTS_DIR}", file=sys.stderr)
        return stats
    for pdir in sorted(PROJECTS_DIR.iterdir()):
        mpath = pdir / "manifest.yaml"
        if not mpath.is_file():
            continue
        try:
            m = Manifest.load(mpath)
        except Exception:
            continue
        stats["scanned"] += 1
        alb = m.data.get("album", {})
        changed = False

        if alb.get("festival") and not alb.get("festival_id"):
            fid = entities.festival_slug(alb["festival"])
            print(f"[festival] {pdir.name}: {alb['festival']!r} -> {fid}")
            if apply:
                alb["festival_id"] = fid
            changed = True
            stats["festival_id"] += 1

        if artists and alb.get("artist") and not alb.get("artist_id"):
            hit = _resolve_artist_id(alb["artist"])
            if hit:
                aid, label = hit
                print(f"[artist]   {pdir.name}: {alb['artist']!r} -> {aid} ({label})")
                if apply:
                    alb["artist_id"] = aid
                changed = True
                stats["artist_id"] += 1
            else:
                print(f"[artist?]  {pdir.name}: {alb['artist']!r} -> pas de correspondance exacte, ignoré")
                stats["artist_skipped"] += 1

        if changed and apply:
            m.save()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="écrit les manifests (sinon dry-run)")
    ap.add_argument("--artists", action="store_true", help="résout aussi les artist_id via Deezer")
    args = ap.parse_args()
    stats = run(apply=args.apply, artists=args.artists)
    mode = "APPLIQUÉ" if args.apply else "DRY-RUN (rien écrit)"
    print(f"\n{mode} — {stats}")


if __name__ == "__main__":
    main()
