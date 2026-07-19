"""Entités canoniques suivables, liées aux albums.

Un « post » = une fiche album. Chaque album est relié à des entités que
l'utilisateur peut suivre :
- **artiste** : l'artiste principal (id Deezer canonique) + les invités ;
- **festival** : événement / tournée (slug dérivé du libellé) ;
- **lieu**    : salle / venue (slug dérivé du libellé).

Ces liens alimentent les pages auto (`/artist`, `/festival`, `/venue`), le
système de suivi (`follows`) et, plus tard, les notifications et le feed.

Principe (aligné sur `suggest.py`) : l'**id** est la clé de regroupement, le
**libellé** n'est qu'un rendu figé. Deux albums qui pointent le même artiste
Deezer se retrouvent sur la même page sans dépendre de l'orthographe saisie.
"""
from __future__ import annotations

from typing import Any

from .manifest import slugify

# `user` est un type de suivi valide (voir follows.py) mais n'est pas dérivé
# d'un album : on ne l'inclut pas dans `album_entities`.
ENTITY_TYPES = {"artist", "festival", "venue"}
FOLLOW_TYPES = ENTITY_TYPES | {"user"}


def festival_slug(label: str) -> str:
    return slugify(label)


def venue_slug(label: str) -> str:
    return slugify(label)


def entity_id_for(etype: str, label: str) -> str:
    """Slug canonique d'un festival/lieu à partir de son libellé libre."""
    if etype == "festival":
        return festival_slug(label)
    if etype == "venue":
        return venue_slug(label)
    return (label or "").strip()


def album_entities(album: dict[str, Any]) -> list[dict]:
    """Liste dédupliquée des entités `{type, id, label}` liées à un album.

    `album` = le sous-dict `manifest['album']`. Best-effort : un champ absent
    est simplement ignoré (un vieil album sans `artist_id` n'expose aucun lien
    artiste tant qu'un gestionnaire ne l'a pas renseigné).
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, eid: Any, label: str) -> None:
        eid = "" if eid is None else str(eid).strip()
        label = (label or "").strip()
        if not eid or not label:
            return
        key = (kind, eid)
        if key in seen:
            return
        seen.add(key)
        out.append({"type": kind, "id": eid, "label": label})

    # Artiste principal (id Deezer + libellé).
    add("artist", album.get("artist_id"), album.get("artist", ""))

    # Invités / autres artistes impliqués.
    for g in album.get("guests", []) or []:
        if isinstance(g, dict):
            add("artist", g.get("id"), g.get("name") or g.get("label") or "")

    # Festival / tournée. `festival_id` explicite sinon dérivé du libellé —
    # ainsi un album antérieur au champ id reste regroupé correctement.
    if album.get("festival"):
        fid = album.get("festival_id") or festival_slug(album["festival"])
        add("festival", fid, album["festival"])

    # Lieu / salle (slug dérivé, pas de source externe).
    if album.get("venue"):
        add("venue", venue_slug(album["venue"]), album["venue"])

    return out


def entity_keys(album: dict[str, Any]) -> set[str]:
    """Ensemble de clés `"type:id"` d'un album — filtrage rapide côté page."""
    return {f"{e['type']}:{e['id']}" for e in album_entities(album)}
