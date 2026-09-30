"""Audit des pochettes : détecte les incohérences entre base sociale, manifest,
fichiers et médias. LECTURE SEULE — ne modifie rien.

    APP_ENV=prod python -m backend.audit_covers [--json]

Le manifest et les médias sont partagés entre prod et preprod ; la base sociale
est propre à chaque environnement. Les écarts « manifest ≠ base » ne sont donc
signalés que pour les albums que l'environnement audité possède
(`covers.owns_shared_files`). Origine : incident 2026-09-26 (vitrine ≠ fiche).
"""
from __future__ import annotations

import hashlib
import json
import sys

from mutagen.id3 import ID3

from .covers import IMAGE_KINDS, cover_file, owns_shared_files, top_cover
from .db import _APP_ENV as APP_ENV, get_conn
from .manifest import PROJECTS_DIR, Manifest

KINDS = ("cover", *IMAGE_KINDS)


def _sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _content(path) -> str | None:
    try:
        return _sha(path.read_bytes())
    except OSError:
        return None


def _first_apic(slug: str) -> bytes | None:
    audio = PROJECTS_DIR / slug / "build" / "audio"
    for mp3 in sorted(audio.glob("*.mp3")) if audio.exists() else []:
        try:
            apics = ID3(str(mp3)).getall("APIC")
        except Exception:
            continue
        return apics[0].data if apics else b""
    return None  # pas de MP3


def audit() -> list[dict]:
    issues: list[dict] = []

    def add(slug, code, detail):
        issues.append({"slug": slug, "code": code, "detail": detail})

    slugs = set()
    with get_conn() as conn:
        for pdir in sorted(PROJECTS_DIR.iterdir()):
            mpath = pdir / "manifest.yaml"
            if not mpath.is_file():
                continue
            slug = pdir.name
            slugs.add(slug)
            try:
                data = Manifest.load(mpath).data
            except Exception as exc:
                add(slug, "manifest_illisible", str(exc))
                continue
            album = data.get("album") or {}
            owned = owns_shared_files(data)
            for kind in KINDS:
                win = top_cover(conn, slug, kind)
                rel = album.get(kind) or ""
                win_rel = (f"artwork/covers/{win['file_key']}_{kind}{win['cover_ext']}"
                           if win else "")
                if win and not cover_file(slug, win["file_key"], win["cover_ext"], kind).exists():
                    add(slug, f"{kind}_gagnante_absente", f"id {win['id']} → {win_rel}")
                if rel and not (pdir / rel).exists():
                    add(slug, f"{kind}_manifest_absent", rel)
                # Deux chemins pour le même contenu (ex. `legacy_cover.*` après
                # migration) ne se voient pas : seul un contenu différent compte.
                if owned and win and rel != win_rel and _content(pdir / rel) != _content(pdir / win_rel):
                    add(slug, f"{kind}_manifest_diverge",
                        f"manifest={rel or '∅'} ≠ gagnante id {win['id']}={win_rel}")
            # Pochette intégrée aux MP3 (vue par Jellyfin/Finamp).
            win = top_cover(conn, slug, "cover")
            if owned and win and not album.get("per_track_covers") \
                    and data.get("published", True):
                src = cover_file(slug, win["file_key"], win["cover_ext"])
                apic = _first_apic(slug)
                if src.exists() and apic is not None and _sha(apic) != _sha(src.read_bytes()):
                    add(slug, "apic_diverge",
                        "pochette intégrée aux MP3 ≠ gagnante" if apic else "MP3 sans pochette intégrée")
            # Lignes dont le fichier a disparu (hors gagnantes, déjà signalées).
            for r in conn.execute("SELECT id, file_key, cover_ext, kind FROM covers WHERE slug=?", (slug,)):
                if not cover_file(slug, r["file_key"], r["cover_ext"], r["kind"]).exists():
                    add(slug, "ligne_sans_fichier", f"id {r['id']} ({r['kind']})")
        for r in conn.execute("SELECT DISTINCT slug FROM covers"):
            if r["slug"] not in slugs:
                add(r["slug"], "ligne_orpheline", "pochettes en base pour un album inexistant")
    return issues


def main() -> int:
    issues = audit()
    if "--json" in sys.argv:
        print(json.dumps(issues, ensure_ascii=False, indent=2))
    else:
        print(f"Audit pochettes — environnement {APP_ENV} — {len(issues)} incohérence(s)")
        for i in issues:
            print(f"  {i['slug']:<45} {i['code']:<24} {i['detail']}")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
