"""Vidéo d'un album existant : rattacher un concert complet, ou redécouper.

Le fichier arrive par l'upload chunké de l'import (`/api/import/upload/*`,
jusqu'à 16 Go, reprise après coupure) ; cette route ne fait que le consommer
depuis le staging. Deux modes, choisis par le gestionnaire :

- `keep`  : les MP3 ne bougent pas. La vidéo est rangée à part
  (`source/video_attached<ext>`, clé `source.video_attached` du manifest) et
  ré-encodée **en entier** en MP4 concert complet par la file de rendu. Elle
  n'a aucun lien avec les timecodes des pistes : l'audio de l'album peut venir
  d'une autre captation.
- `recut` : la vidéo devient le nouveau master. Les coupes existantes sont
  supprimées (titres conservés), l'audio est ré-extrait et le front enchaîne
  sur la préparation (détection IA) puis l'éditeur, comme pour un import neuf.
  Le rendu qui suit est forcé (album publié → `republish`), donc les MP3 sont
  refaits depuis la nouvelle source.

⚠️ Volume partagé prod/preprod : un environnement n'écrit que les albums qu'il
possède (`covers.owns_shared_files`). Sans ce garde-fou, un essai en preprod
remplacerait le master d'un album de prod.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import import_album as imp
from . import renderqueue
from .auth import require_gestionnaire
from .covers import owns_shared_files
from .db import get_conn
from .manifest import PROJECTS_DIR, Manifest
from .pipeline import download
from .pipeline.render import is_external

log = logging.getLogger(__name__)
router = APIRouter()

ATTACHED_STEM = "video_attached"


class AlbumVideoIn(BaseModel):
    token: str    # staging de l'upload chunké
    file: str     # nom du fichier déposé dans le staging
    mode: str     # "keep" | "recut"


def _staged_video(token: str, name: str) -> Path:
    staging = imp._staging(token)
    src = staging / "files" / Path(name).name
    if not src.is_file():
        raise HTTPException(400, "fichier absent du dépôt")
    if src.suffix.lower() not in imp.VIDEO_EXT:
        raise HTTPException(400, "format vidéo attendu (MP4, MOV, MKV, WebM, M4V)")
    return src


def _drop_auto_thumbnails(slug: str) -> None:
    """La miniature automatique est une image de l'ANCIENNE vidéo : on la retire
    pour qu'elle soit régénérée depuis la nouvelle. Les miniatures proposées à
    la main restent."""
    with get_conn() as conn:
        conn.execute("DELETE FROM covers WHERE slug=? AND kind='thumbnail' AND auto=1",
                     (slug,))


def _attach(project_dir: Path, m: Manifest, src: Path) -> None:
    source_dir = project_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    for old in source_dir.glob(f"{ATTACHED_STEM}.*"):
        old.unlink(missing_ok=True)
    dest = source_dir / f"{ATTACHED_STEM}{src.suffix.lower()}"
    shutil.move(str(src), str(dest))
    m.data.setdefault("source", {})["video_attached"] = f"source/{dest.name}"
    m.set_state("video", "pending")
    # Changement technique, pas une édition du contenu (cf. claude/CLAUDE.md).
    m.save(touch=False)


def _recut(project_dir: Path, m: Manifest, src: Path) -> None:
    source = m.data.setdefault("source", {})
    master = project_dir / (source.get("master_mkv") or "source/master.mkv")
    master.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(master))
    source["master_mkv"] = str(master.relative_to(project_dir))
    source["media"] = "video"
    # La vidéo rattachée et l'ancien master audio n'ont plus d'objet : le son
    # vient désormais du nouveau master.
    for key in ("video_attached", "master_audio"):
        rel = source.pop(key, None)
        if rel:
            (project_dir / rel).unlink(missing_ok=True)
    wav = project_dir / (source.get("master_wav") or "source/master.wav")
    source["master_wav"] = str(wav.relative_to(project_dir))
    download.extract_wav(master, wav)
    source["duration"] = download._wav_seconds(wav)
    # Dérivés de l'ancien audio : régénérés par la préparation.
    for name in ("preview.mp3", "waveform.dat"):
        (master.parent / name).unlink(missing_ok=True)
    for t in m.tracks:
        t.pop("start", None)
        t.pop("end", None)
        t["locked"] = False
    m.data["auto_setlist"] = False
    m.data["rerender_pending"] = True   # force le rendu (cf. main.start_render)
    m.set_state("download", "done")
    for stage in ("waveform", "ai_markers", "render", "tags", "artwork", "disc", "video"):
        m.set_state(stage, "pending")
    m.save(touch=True)   # nouvelle source choisie par un humain


@router.post("/api/albums/{slug}/video")
def album_video(slug: str, payload: AlbumVideoIn,
                identity: dict = Depends(require_gestionnaire)) -> dict:
    if payload.mode not in ("keep", "recut"):
        raise HTTPException(400, "mode inconnu (keep ou recut)")
    project_dir = PROJECTS_DIR / slug
    mpath = project_dir / "manifest.yaml"
    if not mpath.exists():
        raise HTTPException(404, "album introuvable")
    m = Manifest.load(mpath)
    if not owns_shared_files(m.data):
        raise HTTPException(409, "album géré par l'autre environnement : "
                                 "importez la vidéo depuis celui-ci")
    if renderqueue.active_kind(slug):
        raise HTTPException(409, "un rendu est déjà en cours pour cet album")
    if payload.mode == "recut" and any(is_external(t) for t in m.tracks):
        raise HTTPException(409, "cet album contient des pistes ajoutées depuis un "
                                 "lien : le redécoupage n'est pas possible")
    src = _staged_video(payload.token, payload.file)

    _drop_auto_thumbnails(slug)
    if payload.mode == "keep":
        _attach(project_dir, m, src)
        renderqueue.enqueue_video(slug, republish=True,
                                  requested_by=identity.get("username", ""))
    else:
        try:
            _recut(project_dir, m, src)
        except Exception as exc:
            log.exception("redécoupage de %s impossible", slug)
            raise HTTPException(500, f"extraction de l'audio impossible : {exc}")
    shutil.rmtree(imp._staging(payload.token), ignore_errors=True)
    # `recut` : le front enchaîne sur /app#slug (préparation puis éditeur).
    return {"ok": True, "slug": slug, "mode": payload.mode}
