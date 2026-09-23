"""Backfill : MP4 concert complet égaré dans `build/video` -> `build/video-full`.

Du 2026-08-13 (retrait des clips par piste de la synchronisation Jellyfin) au
2026-09-18, le rendu écrivait le concert complet dans `build/video/`, seul
`build/video-full/` étant exposé à Jellyfin. Les albums produits sur cette
période ont donc un MP4 valide et complet, mais invisible dans la bibliothèque —
et sans miniature ni bouton de téléchargement, ces deux fonctions lisant elles
aussi `build/video-full`.

La migration **déplace** le fichier (même volume : pas de copie de plusieurs Go,
pas de réencodage) et le renomme aux conventions Jellyfin, images sidecar
comprises. Idempotente : un album déjà en place n'est pas touché.

⚠️ Le volume albums est partagé prod+preprod : la migration vaut pour les deux
environnements et n'a besoin d'être passée qu'une fois.

Usage (depuis la racine du projet) :
    python -m backend.migrate_video_full [--dry-run]
"""
from __future__ import annotations

import sys

from .manifest import PROJECTS_DIR, Manifest
from .pipeline.render import adopt_existing_video, video_dir, video_filename


def migrate(dry_run: bool = False) -> list[tuple[str, str]]:
    """Renvoie la liste des `(slug, nouveau nom)` migrés (ou à migrer)."""
    moved: list[tuple[str, str]] = []
    for pdir in sorted(PROJECTS_DIR.iterdir()):
        mpath = pdir / "manifest.yaml"
        if not mpath.is_file():
            continue
        try:
            m = Manifest.load(mpath)
        except Exception as exc:
            print(f"  ! {pdir.name} : manifeste illisible ({exc})")
            continue
        name = video_filename(m, pdir.name)
        if (video_dir(pdir) / name).exists():
            continue
        legacy = sorted((pdir / "build" / "video").glob("*.mp4"))
        stray = sorted(video_dir(pdir).glob("*.mp4")) if video_dir(pdir).exists() else []
        if not legacy and not stray:
            continue
        if len(legacy) + len(stray) > 1:
            # Plusieurs MP4 : on ne devine pas lequel est le concert complet.
            print(f"  ! {pdir.name} : {len(legacy) + len(stray)} MP4, migration manuelle")
            continue
        if not dry_run and adopt_existing_video(pdir, name) is None:
            continue
        moved.append((pdir.name, name))
    return moved


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    print(f"Albums : {PROJECTS_DIR}")
    print("Mode : DRY-RUN (aucune écriture)" if dry else "Mode : écriture")
    rows = migrate(dry_run=dry)
    for slug, name in rows:
        print(f"  {slug} -> build/video-full/{name}")
    print(f"{len(rows)} album(s) {'à migrer' if dry else 'migré(s)'}.")
