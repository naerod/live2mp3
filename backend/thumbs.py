"""Vignettes dérivées des pochettes — le catalogue ne sert plus l'original.

Une pochette pèse jusqu'à 5 Mo (PNG pleine résolution), pour un affichage en
grille de 200 px : servir l'original était le poste de lenteur n°1 de la
vitrine. On génère donc à la demande un dérivé WebP à la largeur demandée,
mis en cache sur disque à côté de l'album.

Le nom du cache embarque le mtime de la source : remplacer une pochette change
le nom du dérivé, donc aucune invalidation à gérer, et les dérivés périmés du
même album sont balayés à la génération suivante.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

# Largeurs autorisées — whitelist stricte : un `?w=` libre laisserait n'importe
# qui remplir le disque de dérivés.
WIDTHS = (160, 320, 640, 1024)
QUALITY = 78


def thumbs_dir(src: Path) -> Path:
    return src.parent / ".thumbs"


def _key(src: Path) -> str:
    st = src.stat()
    h = hashlib.sha1(f"{src.name}:{int(st.st_mtime)}:{st.st_size}".encode())
    return h.hexdigest()[:12]


def _purge_stale(dest_dir: Path, name: str, keep: str) -> None:
    """Supprime les dérivés d'une même pochette issus d'une version antérieure."""
    prefix = f"{name}_"
    for p in dest_dir.glob(f"{prefix}*.webp"):
        if not p.name.startswith(f"{prefix}{keep}_"):
            try:
                p.unlink()
            except OSError:
                pass


def derive(src: Path, width: int) -> Path | None:
    """Vignette WebP de `src` à `width` px de large (cache disque).

    Retourne `None` si la largeur n'est pas autorisée, si la source est déjà
    plus petite que la cible (inutile de ré-encoder) ou si le décodage échoue —
    l'appelant retombe alors sur le fichier d'origine.
    """
    if width not in WIDTHS:
        return None
    try:
        key = _key(src)
    except OSError:
        return None

    stem = src.stem
    dest_dir = thumbs_dir(src)
    dest = dest_dir / f"{stem}_{key}_{width}.webp"
    if dest.exists():
        return dest

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            # exif_transpose : certaines pochettes viennent d'un appareil photo
            # et portent une orientation EXIF que WebP ne reconduit pas.
            im = ImageOps.exif_transpose(im)
            if im.width <= width:
                return None
            im = im.convert("RGB")
            im.thumbnail((width, width * 4), Image.LANCZOS)
            # Écriture atomique : deux workers peuvent viser le même dérivé.
            tmp = dest.with_suffix(f".{os.getpid()}.tmp")
            im.save(tmp, "WEBP", quality=QUALITY, method=4)
            os.replace(tmp, dest)
    except Exception as e:  # pragma: no cover - dépend du fichier source
        log.warning("thumb %s w=%s: %s", src, width, e)
        return None

    _purge_stale(dest_dir, stem, key)
    return dest


def warm_all(widths: tuple[int, ...] = (320, 640)) -> tuple[int, int]:
    """Pré-génère les dérivés de toutes les pochettes connues.

    À lancer après un déploiement : sans ça, c'est le premier visiteur qui paie
    le redimensionnement de 88 pochettes (dont des PNG de 5 Mo), et la vitrine
    est plus lente qu'avant le temps de remplir le cache.
    """
    from .manifest import PROJECTS_DIR

    done = failed = 0
    for pdir in sorted(PROJECTS_DIR.iterdir()):
        art = pdir / "artwork"
        if not art.is_dir():
            continue
        srcs = [p for p in art.glob("*") if p.is_file()]
        srcs += [p for p in (art / "covers").glob("*") if p.is_file()]
        for src in srcs:
            if src.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                continue
            for w in widths:
                if derive(src, w):
                    done += 1
                else:
                    failed += 1
    return done, failed


if __name__ == "__main__":  # pragma: no cover
    d, f = warm_all()
    print(f"{d} vignette(s) générée(s), {f} ignorée(s)/échec")
