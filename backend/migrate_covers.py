"""Backfill : pochette unique historique -> collection de pochettes créditées.

Avant cette feature, un album portait au plus une `artwork/cover.*` (pointée par
`album.cover`) et une `artwork/tray_card.pdf`. On les enregistre comme la
première proposition de l'album, créditée à `meta.imported_by`.

Idempotent, et pensé pour le volume albums partagé prod+preprod : les fichiers
sont *copiés* sous la clé fixe `legacy`, jamais déplacés. Les deux bases peuvent
donc être migrées chacune de leur côté et pointer vers les mêmes fichiers, et
les originaux restent en place — un rollback n'a rien à restaurer.

Usage (depuis la racine du projet) :
    APP_ENV=preprod python -m backend.migrate_covers [--dry-run]
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone

from .db import DB_PATH, get_conn, init_db
from .manifest import PROJECTS_DIR, Manifest
from .covers import covers_dir, cover_file, traycard_file

LEGACY_KEY = "legacy"
FALLBACK_OWNER = "naerod"


def migrate(dry_run: bool = False) -> int:
    init_db()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    done = 0
    for pdir in sorted(PROJECTS_DIR.iterdir()):
        mpath = pdir / "manifest.yaml"
        if not mpath.is_file():
            continue
        slug = pdir.name
        try:
            m = Manifest.load(mpath)
        except Exception as e:
            print(f"  ! {slug}: manifest illisible ({e})")
            continue

        cover_rel = m.data.get("album", {}).get("cover")
        # Déjà migré : le pointeur vise la nouvelle arborescence.
        if not cover_rel or cover_rel.startswith("artwork/covers/"):
            continue
        src = pdir / cover_rel
        if not src.exists():
            print(f"  ! {slug}: album.cover pointe vers un fichier absent ({cover_rel})")
            continue

        with get_conn() as conn:
            if conn.execute(
                "SELECT 1 FROM covers WHERE slug=? AND file_key=?", (slug, LEGACY_KEY)
            ).fetchone():
                continue
            owner = m.data.get("meta", {}).get("imported_by") or FALLBACK_OWNER
            cext = src.suffix.lower()
            tray = pdir / "artwork" / "tray_card.pdf"
            text = ".pdf" if tray.exists() else ""
            print(f"  - {slug}: cover{cext} -> {owner}" + (" (+ tray card)" if text else ""))
            if dry_run:
                done += 1
                continue
            conn.execute(
                "INSERT INTO covers(slug, username, file_key, cover_ext, traycard_ext, "
                "caption, pinned, created_at, updated_at) VALUES(?,?,?,?,?,'',0,?,?)",
                (slug, owner, LEGACY_KEY, cext, text, now, now),
            )
            covers_dir(slug).mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, cover_file(slug, LEGACY_KEY, cext))
            if text:
                shutil.copy2(tray, traycard_file(slug, LEGACY_KEY, text))
            # Repointer le manifest : `album.cover` désigne désormais la gagnante
            # dans la nouvelle arborescence, comme le fait `_on_covers_changed`.
            m.data["album"]["cover"] = f"artwork/covers/{LEGACY_KEY}_cover{cext}"
            # Repointage technique dans une migration : pas de `touch`
            # (cf. incident 2026-09-21).
            m.save(touch=False)
        done += 1
    return done


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    print(f"Base : {DB_PATH}")
    print(f"Albums : {PROJECTS_DIR}")
    print("Mode : DRY-RUN (aucune écriture)" if dry else "Mode : écriture")
    n = migrate(dry_run=dry)
    print(f"{n} album(s) {'à migrer' if dry else 'migré(s)'}.")
