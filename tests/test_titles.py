"""Titre suggéré d'un concert : `Live in Ville/lieu/festival AAAA (Tournée - bonus)`,
sans préfixe « Artiste - » (l'artiste a son propre champ)."""
from backend.titles import suggest_concert_title


def test_full_format_has_no_artist_prefix():
    alb = {"artist": "Twenty One Pilots", "city": "Mexico City", "date": "2025-03-14",
           "tour": "The Clancy World Tour", "subtitle": "Bonus"}
    assert suggest_concert_title(alb) == "Live in Mexico City 2025 (The Clancy World Tour - Bonus)"


def test_tour_only_and_subtitle_only():
    base = {"artist": "U2", "city": "New York", "date": "2014-12-01"}
    assert suggest_concert_title({**base, "tour": "Innocence"}) == "Live in New York 2014 (Innocence)"
    assert suggest_concert_title({**base, "subtitle": "Rappel"}) == "Live in New York 2014 (Rappel)"
    assert suggest_concert_title(base) == "Live in New York 2014"


def test_place_falls_back_to_venue_then_festival():
    assert suggest_concert_title({"venue": "Stade de France", "date": "2026-09-05"}) \
        == "Live in Stade de France 2026"
    assert suggest_concert_title({"festival": "Main Square Festival", "date": "2026-07-05"}) \
        == "Live in Main Square Festival 2026"
    # La ville l'emporte sur le lieu et le festival.
    assert suggest_concert_title({"city": "Arras", "venue": "Citadelle",
                                  "festival": "Main Square Festival", "date": "2026-07-05"}) \
        == "Live in Arras 2026"


def test_no_place_no_year():
    assert suggest_concert_title({"artist": "Coldplay"}) == "Live"
    assert suggest_concert_title({"artist": "Coldplay", "date": "2024"}) == "Live 2024"
