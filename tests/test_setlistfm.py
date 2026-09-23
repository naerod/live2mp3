"""Setlist officielle via setlist.fm — aucun appel réseau (API simulée)."""
from __future__ import annotations

import pytest

from backend import linktool, setlistfm

# Réponse réelle de l'API pour U2 au Times Square le 01-12-2014, réduite aux
# champs utilisés. Deux setlists le même soir : la seconde n'en couvre qu'une
# partie — c'est le cas que `lookup` doit départager.
RAW = [
    {
        "artist": {"name": "U2"},
        "venue": {"name": "Times Square",
                  "city": {"name": "New York", "country": {"code": "US"}}},
        "tour": {"name": "World AIDS Day"},
        "url": "https://www.setlist.fm/setlist/u2/2014/times-square-4bcde33a.html",
        "sets": {"set": [{"song": [
            {"name": "Beautiful Day", "with": {"name": "Chris Martin"}},
            {"name": "With or Without You", "with": {"name": "Chris Martin"}},
            {"name": "Where the Streets Have No Name",
             "with": {"name": "Bruce Springsteen"}},
            {"name": "I Still Haven't Found What I'm Looking For",
             "with": {"name": "Bruce Springsteen"}},
        ]}]},
    },
    {
        "artist": {"name": "U2 with Bruce Springsteen"},
        "venue": {"name": "Times Square",
                  "city": {"name": "New York", "country": {"code": "US"}}},
        "url": "https://www.setlist.fm/setlist/u2-with-bruce/2014/ts-2b75344e.html",
        "sets": {"set": [{"song": [
            {"name": "Where the Streets Have No Name"},
            {"name": "I Still Haven't Found What I'm Looking For"},
        ]}]},
    },
]


@pytest.fixture(autouse=True)
def _clear_cache():
    setlistfm._cache.clear()
    yield
    setlistfm._cache.clear()


def test_to_api_date():
    assert setlistfm.to_api_date("2014-12-01") == "01-12-2014"
    assert setlistfm.to_api_date("1 déc 2014") is None
    assert setlistfm.to_api_date("") is None


def test_parse_setlist_titles_and_guests():
    p = setlistfm.parse_setlist(RAW[0])
    assert [t["title"] for t in p["tracks"]] == [
        "Beautiful Day", "With or Without You",
        "Where the Streets Have No Name",
        "I Still Haven't Found What I'm Looking For"]
    # L'invité devient l'artiste de la piste (convention de l'outil).
    assert p["tracks"][0]["artist"] == "U2 with Chris Martin"
    assert p["tracks"][2]["artist"] == "U2 with Bruce Springsteen"
    assert [t["n"] for t in p["tracks"]] == [1, 2, 3, 4]
    assert p["venue"] == "Times Square" and p["city"] == "New York"
    assert p["tour"] == "World AIDS Day"
    assert p["url"].startswith("https://www.setlist.fm/")


def test_parse_setlist_skips_taped_songs():
    raw = {"artist": {"name": "X"}, "sets": {"set": [{"song": [
        {"name": "Intro", "tape": True},      # diffusé par la sono
        {"name": "Vraie chanson"},
    ]}]}}
    p = setlistfm.parse_setlist(raw)
    assert [t["title"] for t in p["tracks"]] == ["Vraie chanson"]


def test_lookup_prefers_most_complete_setlist(monkeypatch):
    monkeypatch.setattr(setlistfm, "_search", lambda a, d: RAW)
    sl = setlistfm.lookup("U2", "2014-12-01")
    assert len(sl["tracks"]) == 4          # pas la version à 2 titres
    assert sl["artist"] == "U2"


def test_lookup_without_key_raises(monkeypatch):
    monkeypatch.delenv("SETLISTFM_API_KEY", raising=False)
    with pytest.raises(setlistfm.SetlistUnavailable):
        setlistfm.lookup("U2", "2014-12-01")


def test_lookup_needs_artist_and_valid_date(monkeypatch):
    monkeypatch.setattr(setlistfm, "_search",
                        lambda a, d: pytest.fail("ne doit pas appeler l'API"))
    assert setlistfm.lookup("", "2014-12-01") is None
    assert setlistfm.lookup("U2", "date inconnue") is None


def test_lookup_caches(monkeypatch):
    calls = []
    def fake(a, d):
        calls.append((a, d))
        return RAW
    monkeypatch.setattr(setlistfm, "_search", fake)
    setlistfm.lookup("U2", "2014-12-01")
    setlistfm.lookup("U2", "2014-12-01")
    assert len(calls) == 1                 # quota : 1440 req/jour


# --- Fusion dans la suggestion d'analyse -----------------------------------
def test_apply_setlistfm_fills_tracks(monkeypatch):
    monkeypatch.setattr(setlistfm, "_search", lambda a, d: RAW)
    sug = {"artist": "U2", "date": "2014-12-01", "venue": "", "tracks": []}
    src = linktool.apply_setlistfm(sug)
    assert src and src["tracks"] == 4
    assert len(sug["tracks"]) == 4
    assert sug["tracks"][0]["artist"] == "U2 with Chris Martin"
    assert sug["venue"] == "Times Square"
    assert sug["setlistfm_url"].startswith("https://www.setlist.fm/")


def test_apply_setlistfm_keeps_timecodes_and_takes_titles(monkeypatch):
    """Chapitres présents et même nombre de titres : on garde les timecodes."""
    monkeypatch.setattr(setlistfm, "_search", lambda a, d: RAW)
    sug = {"artist": "U2", "date": "2014-12-01", "tracks": [
        {"n": i + 1, "title": f"Chapitre {i+1}", "artist": None,
         "start": i * 300.0, "end": (i + 1) * 300.0} for i in range(4)]}
    linktool.apply_setlistfm(sug)
    assert sug["tracks"][0]["start"] == 0.0          # timecode conservé
    assert sug["tracks"][0]["title"] == "Beautiful Day"   # titre officiel
    assert sug["tracks"][0]["artist"] == "U2 with Chris Martin"


def test_apply_setlistfm_refuses_when_counts_differ(monkeypatch):
    """Timecodes fiables mais nombre différent : on n'écrase rien."""
    monkeypatch.setattr(setlistfm, "_search", lambda a, d: RAW)
    sug = {"artist": "U2", "date": "2014-12-01", "tracks": [
        {"n": 1, "title": "Chapitre 1", "start": 0.0, "end": 300.0}]}
    assert linktool.apply_setlistfm(sug) is None
    assert sug["tracks"][0]["title"] == "Chapitre 1"
    assert "setlistfm_url" not in sug


def test_apply_setlistfm_survives_api_down(monkeypatch):
    """API injoignable : l'analyse continue sans setlist officielle."""
    def boom(a, d):
        raise setlistfm.SetlistUnavailable("réseau")
    monkeypatch.setattr(setlistfm, "_search", boom)
    sug = {"artist": "U2", "date": "2014-12-01", "tracks": []}
    assert linktool.apply_setlistfm(sug) is None
    assert sug["tracks"] == []


# --- Recherche tolérante (artiste/date devinés par l'IA) -------------------
# Deux concerts de la même tournée à quinze jours d'écart : la date proposée
# par l'IA (le 20) ne correspond à aucun des deux. Seule une preuve
# (même date, même salle, même ville) doit permettre un rattachement.
YEAR_RAW = [
    {"artist": {"name": "twenty one pilots"}, "eventDate": "05-07-2026",
     "venue": {"name": "La Citadelle",
               "city": {"name": "Arras", "country": {"code": "FR"}}},
     "url": "https://www.setlist.fm/setlist/top/2026/la-citadelle-43416fdf.html",
     "sets": {"set": [{"song": [{"name": "Overcompensate"}, {"name": "Heathens"}]}]}},
    {"artist": {"name": "twenty one pilots"}, "eventDate": "21-07-2026",
     "venue": {"name": "Grande Scène",
               "city": {"name": "Carhaix", "country": {"code": "FR"}}},
     "url": "https://www.setlist.fm/setlist/top/2026/grande-scene-1234abcd.html",
     "sets": {"set": [{"song": [{"name": "Jumpsuit"}]}]}},
]


def test_from_api_date():
    assert setlistfm.from_api_date("05-07-2026") == "2026-07-05"
    assert setlistfm.from_api_date("") == ""


def test_parse_setlist_exposes_iso_date():
    assert setlistfm.parse_setlist(YEAR_RAW[0])["date"] == "2026-07-05"


def _stub_year(monkeypatch):
    monkeypatch.setattr(setlistfm, "_search", lambda a, d: [])
    monkeypatch.setattr(
        setlistfm, "_search_raw",
        lambda params: YEAR_RAW if params.get("artistName", "").casefold()
        == "twenty one pilots" else [])


def test_lookup_flexible_ignores_festival_name_and_finds_artist(monkeypatch):
    """« Main Square 2026 - Twenty One Pilots » : le festival n'est pas l'artiste."""
    _stub_year(monkeypatch)
    sl = setlistfm.lookup_flexible(["Main Square 2026", "Twenty One Pilots"],
                                   "2026-07-20", city="Arras")
    assert sl and sl["date"] == "2026-07-05" and sl["venue"] == "La Citadelle"


def test_lookup_flexible_refuses_a_merely_close_date(monkeypatch):
    """Sans preuve (salle/ville/date exacte), on ne propose rien : une setlist
    fausse serait pire que pas de setlist."""
    _stub_year(monkeypatch)
    assert setlistfm.lookup_flexible(
        ["Main Square 2026", "Twenty One Pilots"], "2026-07-20") is None


def test_artist_candidates_from_video_title():
    cands = linktool.artist_candidates(
        {"artist": "Main Square 2026"},
        {"title": "Main Square 2026 - Twenty One Pilots"})
    assert "Twenty One Pilots" in cands
    assert cands[0] == "Main Square 2026"      # la proposition de l'IA d'abord
