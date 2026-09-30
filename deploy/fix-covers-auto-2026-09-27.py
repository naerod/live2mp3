#!/usr/bin/env python3
"""Correctif ponctuel (incident 2026-09-27) : pochettes marquées « automatique » à tort.

`_ensure_auto_cover` créait les lignes manquantes avec username='auto', auto=1.
Pour chaque ligne de ce type (kind='cover') dans la base visée :
  1. la ligne réelle de l'autre environnement (même slug + file_key) fait foi ;
  2. sinon : auteur inconnu (username=''), auto=0 ;
  3. attributions explicites fournies par Dorian (ATTRIB) prioritaires.

Usage : fix-covers-auto-2026-09-27.py <base.db> <base_autre_env.db> [--apply]
"""
import sqlite3, sys

ATTRIB = {"coldplay-2024-06-30": "nathan"}   # pochette de Nathan, importée par Dorian

db, other, apply = sys.argv[1], sys.argv[2], "--apply" in sys.argv
c = sqlite3.connect(db)
o = sqlite3.connect(f"file:{other}?mode=ro", uri=True)
rows = c.execute("SELECT id, slug, file_key FROM covers "
                 "WHERE kind='cover' AND username='auto'").fetchall()
for cid, slug, key in rows:
    real = o.execute("SELECT username, auto FROM covers WHERE slug=? AND file_key=? "
                     "AND kind='cover' AND username NOT IN ('', 'auto')",
                     (slug, key)).fetchone()
    user, auto = real if real else ("", 0)
    if slug in ATTRIB:
        user, auto = ATTRIB[slug], 0
    print(f"{cid:>4} {slug:<32} -> auteur={user or '(inconnu)'} auto={auto}"
          + ("  [autre env]" if real else ""))
    if apply:
        c.execute("UPDATE covers SET username=?, auto=? WHERE id=?", (user, auto, cid))
        if user:
            c.execute("INSERT OR IGNORE INTO profiles(username, display_name, created_at, "
                      "updated_at) VALUES(?, ?, datetime('now'), datetime('now'))", (user, user))
if apply:
    c.commit()
print(f"{len(rows)} ligne(s) {'corrigée(s)' if apply else 'à corriger (dry-run)'}")
