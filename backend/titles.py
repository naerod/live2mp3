"""Composition du nom d'un concert selon le formalisme maison.

Format cible (concerts) :
    Artiste - Live in Ville AAAA (Tournée - Texte bonus)

- « Live in Ville » quand une ville est renseignée ; sinon « Live » seul.
- l'année est dérivée de `album.date` (premier groupe de 4 chiffres).
- la parenthèse regroupe `tour` puis `subtitle` (bonus), séparés par « - » ;
  omise si les deux sont vides.

Cette suggestion n'est jamais imposée : elle pré-remplit le champ titre, que le
gestionnaire peut librement corriger (festivals, plateaux TV, cas particuliers).
"""
from __future__ import annotations

import re


def _year(date: str) -> str:
    m = re.search(r"(\d{4})", date or "")
    return m.group(1) if m else ""


def suggest_concert_title(alb: dict) -> str:
    """Titre suggéré au format maison à partir des champs structurés d'un album."""
    artist = (alb.get("artist") or "").strip()
    city = (alb.get("city") or "").strip()
    tour = (alb.get("tour") or "").strip()
    subtitle = (alb.get("subtitle") or "").strip()
    year = _year(alb.get("date") or "")

    loc = f"Live in {city}" if city else "Live"
    left = f"{artist} - {loc}" if artist else loc
    if year:
        left = f"{left} {year}"
    extra = " - ".join(x for x in (tour, subtitle) if x)
    if extra:
        left = f"{left} ({extra})"
    return left.strip()
