# -*- coding: utf-8 -*-
"""Rattrapage unique : aligne tags et noms de fichiers audio sur le manifeste.

Contexte (2026-09-19). Deux dérives cumulées sur les albums importés :

* les tags ID3 n'avaient jamais été réécrits depuis le manifeste et portaient
  encore le nom du fichier source (« 01. 01_Coldplay_MOON MUSiC »,
  « 02. ma_vie_est_a_toi », « 04. Coldplay (HD) - Everythings Not Lost
  (Glastonbury 2011) ») ;
* la convention du tag titre change : titre **nu** dans TIT2 (Finamp numérote
  déjà ses lignes), numéro conservé dans le **nom de fichier** (ordre du
  concert dans un ZIP trié alphabétiquement).

Le manifeste fait foi pour les titres — il est relu et corrigé à la main, il
est propre. Ce script ne devine aucun titre : il ne fait que le recopier.

**L'appariement fichier↔piste est le seul vrai risque** : le mapping applicatif
(`albumfiles.map_files_to_tracks`) apparie par sous-chaîne et associe « Lost! »
au fichier « Everything's Not Lost », soit deux titres inversés en silence. On
réapparie donc ici de façon stricte, par ordre de confiance décroissant, et on
refuse d'écrire un album dont l'appariement n'est pas une bijection complète.

Usage :
    python retag-track-titles.py                 # dry-run (défaut)
    python retag-track-titles.py --apply         # écrit, après sauvegarde
    python retag-track-titles.py --apply <slug>… # restreint à des albums
S'exécute dans le conteneur `live2mp3-app` (mutagen + /app/projects).
"""
import argparse
import difflib
import glob
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import yaml
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError

sys.path.insert(0, "/app")
from backend.manifest import sanitize_filename  # noqa: E402

PROJECTS = Path("/app/projects")
BACKUP_DIR = Path("/app/projects/.retag-backups")


def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def prefix_n(stem):
    m = re.match(r"^(\d{1,3})[._\s)-]", stem)
    return int(m.group(1)) if m else None


def suffix_n(stem):
    m = re.search(r"-(\d{1,3})$", stem)
    return int(m.group(1)) if m else None


def pair(tracks, files):
    """Retourne (mapping {position: fichier}, stratégie, réserves[]).

    mapping est None si aucune stratégie ne donne une bijection complète.
    """
    n = len(tracks)
    positions = list(range(1, n + 1))
    pref = {f: prefix_n(f.stem) for f in files}

    # 1) Préfixe numérique bijectif. Base 0 tolérée : quelques manifestes
    #    numérotent les pistes à partir de 0.
    if len(files) == n and all(v is not None for v in pref.values()):
        vals = sorted(pref.values())
        for base in (1, 0):
            if vals == list(range(base, base + n)):
                return ({v - base + 1: f for f, v in pref.items()},
                        f"préfixe numérique (base {base})", [])

    # 2) Titre exact normalisé, bijectif.
    by_title, used, ok = {}, set(), True
    for pos, t in enumerate(tracks, 1):
        nt = norm(t.get("title"))
        cands = [f for f in files if f not in used and norm(f.stem) == nt]
        if len(cands) != 1:
            cands = [f for f in files if f not in used
                     and re.search(rf"(^|\W){re.escape(nt)}($|\W)", norm(f.stem))]
        if len(cands) == 1:
            by_title[pos] = cands[0]
            used.add(cands[0])
        else:
            ok = False
    if ok and len(by_title) == n:
        return by_title, "titre exact", []

    # 3) Similarité mutuelle sur le reliquat : chaque paire doit être le
    #    meilleur choix des DEUX côtés. C'est ce qui rattrape les typos
    #    (« Wihout ») et les apostrophes perdues (« Dont ») sans reproduire
    #    l'inversion du mapping par sous-chaîne.
    rest_pos = [p for p in positions if p not in by_title]
    rest_f = [f for f in files if f not in used]
    if rest_pos and rest_f:
        score = {(p, f): difflib.SequenceMatcher(
            None, norm(tracks[p - 1].get("title")), norm(f.stem)).ratio()
            for p in rest_pos for f in rest_f}
        for p in list(rest_pos):
            best_f = max(rest_f, key=lambda f: score[(p, f)])
            if max(rest_pos, key=lambda q: score[(q, best_f)]) == p \
                    and score[(p, best_f)] >= 0.55:
                by_title[p] = best_f
                used.add(best_f)
                rest_f.remove(best_f)
                rest_pos.remove(p)
        # Dernier reliquat 1↔1 : bijection forcée, plus rien à départager.
        if len(rest_pos) == 1 and len(rest_f) == 1:
            by_title[rest_pos[0]] = rest_f[0]
            used.add(rest_f[0])
            rest_pos, rest_f = [], []
        if not rest_pos and len(by_title) == n:
            return by_title, "titre (similarité mutuelle)", []

    # 4) Suffixe numérique (« …-1 » … « …-16 ») : aucun titre exploitable dans
    #    le nom, l'ordre est *supposé* être celui du setlist.
    suf = {f: suffix_n(f.stem) for f in files}
    if len(files) == n and all(v is not None for v in suf.values()):
        if sorted(suf.values()) == list(range(1, n + 1)):
            return ({v: f for f, v in suf.items()},
                    "suffixe numérique",
                    ["ordre déduit du seul suffixe de nom de fichier, "
                     "non recoupé par les titres"])

    # 5) Album partiellement rendu : moins de fichiers que de pistes, mais
    #    des préfixes distincts et cohérents.
    if len(files) < n and all(v is not None for v in pref.values()):
        vals = sorted(pref.values())
        if len(set(vals)) == len(vals) and all(1 <= v <= n for v in vals):
            return ({v: f for f, v in pref.items()},
                    "préfixe numérique (partiel)",
                    [f"{n - len(files)} piste(s) du manifeste sans fichier audio"])

    doubts = []
    if len(files) != n:
        doubts.append(f"{len(files)} fichier(s) pour {n} piste(s)")
    miss = [p for p in positions if p not in by_title]
    if miss:
        doubts.append("pistes non appariées : " + ", ".join(
            f"{p} « {tracks[p-1].get('title')} »" for p in miss[:8]))
    orph = sorted(f.name for f in files if f not in used)
    if orph:
        doubts.append("fichiers non appariés : " + ", ".join(orph[:8]))
    return None, None, doubts


def process(slug, apply):
    mf = PROJECTS / slug / "manifest.yaml"
    audio = PROJECTS / slug / "build" / "audio"
    d = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
    tracks = d.get("tracks") or []
    files = sorted(audio.glob("*.mp3"))
    if not tracks or not files:
        return None
    mapping, strat, doubts = pair(tracks, files)
    alb = d.get("album") or {}
    rep = {"slug": slug, "artist": alb.get("artist"), "album": alb.get("title"),
           "strategy": strat, "doubts": doubts, "tags": [], "renames": [],
           "backup": []}
    if not mapping:
        rep["blocked"] = True
        return rep

    total = len(tracks)
    planned = []
    for pos, f in sorted(mapping.items()):
        title = (tracks[pos - 1].get("title") or "").strip()
        try:
            cur = (EasyID3(str(f)).get("title") or [""])[0]
        except (ID3NoHeaderError, Exception):
            cur = ""
        target_name = f"{pos:02d}. {sanitize_filename(title)}.mp3"
        rep["backup"].append({"file": f.name, "title": cur})
        if cur != title:
            rep["tags"].append({"pos": pos, "from": cur, "to": title})
        if f.name != target_name:
            rep["renames"].append({"from": f.name, "to": target_name})
        planned.append((f, pos, title, target_name))

    if not apply or (not rep["tags"] and not rep["renames"]):
        return rep

    # Tags d'abord (le fichier existe encore sous son nom actuel), renommage
    # ensuite en deux passes pour ne pas écraser une cible homonyme.
    for f, pos, title, _ in planned:
        try:
            tags = EasyID3(str(f))
        except ID3NoHeaderError:
            tags = EasyID3()
            tags.save(str(f))
            tags = EasyID3(str(f))
        tags["title"] = [title]
        tags["tracknumber"] = [f"{pos}/{total}"]
        tags.save()
    pending = []
    for f, pos, title, target in planned:
        if f.name == target:
            continue
        tmp = f.parent / f"._retag_{pos:02d}_{f.name}"
        f.rename(tmp)
        pending.append((tmp, f.parent / target))
    for tmp, final in pending:
        tmp.rename(final)
    return rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slugs", nargs="*")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    slugs = args.slugs or [Path(p).parent.name
                           for p in sorted(glob.glob(str(PROJECTS / "*" / "manifest.yaml")))]
    reports = []
    for slug in slugs:
        if not (PROJECTS / slug / "build" / "audio").exists():
            continue
        r = process(slug, args.apply)
        if r:
            reports.append(r)

    if args.apply:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = BACKUP_DIR / f"retag-{stamp}.json"
        out.write_text(json.dumps(reports, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(f"sauvegarde : {out}", file=sys.stderr)
    print(json.dumps(reports, ensure_ascii=False))


if __name__ == "__main__":
    main()
