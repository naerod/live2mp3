"""Composition du nom d'un concert selon le formalisme maison.

Format cible (concerts), SANS le nom de l'artiste (il a son propre champ et
s'affiche à part sur la fiche, la vitrine et le profil) :
    Live in Ville/lieu/festival AAAA (Tournée - Texte bonus)

- « Live in X » où X est, par ordre de préférence, la ville, le lieu ou le
  festival renseigné ; « Live » seul si aucun des trois ne l'est.
- l'année est dérivée de `album.date` (premier groupe de 4 chiffres).
- la parenthèse regroupe `tour` puis `subtitle` (bonus), séparés par « - » ;
  omise si les deux sont vides.

Cette suggestion n'est jamais imposée : elle pré-remplit le champ titre, que le
gestionnaire peut librement corriger (plateaux TV, compilations, cas particuliers).
Miroir côté navigateur : `composeConcertTitle()` dans `frontend/album.html`.
"""
from __future__ import annotations

import re


def _year(date: str) -> str:
    m = re.search(r"(\d{4})", date or "")
    return m.group(1) if m else ""


def suggest_concert_title(alb: dict) -> str:
    """Titre suggéré au format maison à partir des champs structurés d'un album."""
    place = next(
        ((alb.get(k) or "").strip() for k in ("city", "venue", "festival")
         if (alb.get(k) or "").strip()),
        "",
    )
    tour = (alb.get("tour") or "").strip()
    subtitle = (alb.get("subtitle") or "").strip()
    year = _year(alb.get("date") or "")

    left = f"Live in {place}" if place else "Live"
    if year:
        left = f"{left} {year}"
    extra = " - ".join(x for x in (tour, subtitle) if x)
    if extra:
        left = f"{left} ({extra})"
    return left.strip()
