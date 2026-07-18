"""naming.py — the L3 parser corpus. Strict, total, idempotent."""
from __future__ import annotations

from hypothesis import given, strategies as st

from mlo.naming import (MediaName, clean_title, has_year_stutter, movie_folder,
                        parse_episode, parse_media_name, parse_year,
                        season_folder)

any_text = st.text(
    alphabet=st.characters(blacklist_characters="\x00",
                           min_codepoint=1, max_codepoint=0x10FFFF,
                           blacklist_categories=()),
    max_size=80)


# ── the L3 corpus: years are ONLY parenthesized plausible years ──────────────

def test_year_only_in_parentheses():
    assert parse_year("Inception (2010) 1080p") == 2010
    assert parse_year("Sivaji.The.Boss.(2007).DVDRip") == 2007
    assert parse_year("Movie 2007 rip") is None            # bare digits: never
    assert parse_year("IMG_20180902_101112") is None        # timestamps: never
    assert parse_year("1080p (0450)") is None               # implausible year
    assert parse_year("(2036) future") is None              # out of range
    assert parse_year("Blade Runner 2049 (2017)") == 2017   # year in title ok


def test_last_parenthesized_year_wins():
    assert parse_year("2001 A Space Odyssey (1968)") == 1968
    assert parse_year("(1984) (2019) remaster") == 2019


def test_movie_folder_requires_both_parts():
    assert movie_folder(MediaName("Roja", 1992, None, None)) == "Roja (1992)"
    assert movie_folder(MediaName("", 1992, None, None)) is None
    assert movie_folder(MediaName("Roja", None, None, None)) is None


# ── episodes ─────────────────────────────────────────────────────────────────

def test_episode_patterns():
    assert parse_episode("Friends S05E14 The One Where") == (5, 14)
    assert parse_episode("friends s05e14") == (5, 14)
    assert parse_episode("Show 3x07 title") == (3, 7)
    assert parse_episode("2x4 Lumber Guide") is None        # 1-digit ep: no
    assert parse_episode("Movie (2007)") is None            # year is not 20x07
    assert parse_episode("S01E02") == (1, 2)
    assert parse_episode("house.s1e3.avi") == (1, 3)


def test_parse_media_name_episode():
    n = parse_media_name("Friends.S05E14.The.One.mkv")
    assert n.is_episode and (n.season, n.episode) == (5, 14)
    assert n.title == "Friends"


def test_parse_media_name_movie():
    n = parse_media_name("Sivaji.The.Boss.(2007).1080p.DVDRip.mkv")
    assert n.title == "Sivaji The Boss" and n.year == 2007
    assert not n.is_episode
    assert movie_folder(n) == "Sivaji The Boss (2007)"


def test_parse_media_name_year_but_no_title():
    n = parse_media_name("(2019).mkv")
    assert n.year is None                                   # refuses to guess


def test_release_tags_after_year_vanish_by_construction():
    n = parse_media_name("Inception (2010) [1080p] YIFY x264.mp4")
    assert movie_folder(n) == "Inception (2010)"


# ── totality + idempotence (property) ────────────────────────────────────────

@given(any_text)
def test_parsers_are_total(s):
    parse_year(s)
    parse_episode(s)
    clean_title(s)
    parse_media_name(s + ".mkv")


@given(any_text)
def test_clean_title_idempotent(s):
    once = clean_title(s)
    assert clean_title(once) == once


def test_movie_folder_reparses_to_itself():
    """Routing an already-Jellyfin-named file must not move it again —
    reorganize convergence depends on this."""
    for name in ("Roja (1992)", "Sivaji The Boss (2007)",
                 "2001 A Space Odyssey (1968)"):
        n = parse_media_name(name + ".mkv")
        assert movie_folder(n) == name


def test_season_folder():
    assert season_folder(5) == "Season 05"
    assert season_folder(12) == "Season 12"


# ── C26: year-stutter detector (the narrow disease) ──────────────────────────

def test_has_year_stutter_positives():
    # multiple (Year) suffixes: the observed live-library disease
    assert has_year_stutter("2012 (2012) (2012) (2012) (2012)")
    assert has_year_stutter("(2004) Mystic River (2004) (2004) (2004) (2004)")
    assert has_year_stutter("Mystic River (2004) (2004)")
    # leading (Year) prefix even without repetition
    assert has_year_stutter("(2004) Mystic River")


def test_has_year_stutter_negatives_are_deliberate():
    # canonical Jellyfin folder — MUST NOT trigger (idempotence)
    assert not has_year_stutter("Mystic River (2004)")
    # junk-tagged folder — deliberately tolerated (that tolerance is by design;
    # a router change here would cause mass churn the user rejected)
    assert not has_year_stutter("Mystic River (2004) [1080p BluRay] YIFY")
    assert not has_year_stutter("Inception (2010) [x264]")
    # no year at all
    assert not has_year_stutter("some folder name")
    assert not has_year_stutter("")
    # bare digits are not years (L3 — the ledger says so)
    assert not has_year_stutter("Movie 2007 rip 2008")


def test_movie_folder_dedups_stuttered_year():
    # a MediaName whose parsed title still carries repeated (Year) tokens
    # (parse_media_name only strips the last one) must render canonically
    n = MediaName("Mystic River (2004) (2004) (2004)", 2004, None, None)
    assert movie_folder(n) == "Mystic River (2004)"
    n = MediaName("2012 (2012) (2012) (2012)", 2012, None, None)
    assert movie_folder(n) == "2012 (2012)"


def test_movie_folder_idempotent_on_stuttered_reparse():
    """A stuttered folder, parsed then reformatted, must render clean; a second
    pass through the pipeline must match — the guarantee reorganize convergence
    relies on for C26."""
    for stuttered in (
            "(2004) Mystic River (2004) (2004) (2004) (2004)",
            "2012 (2012) (2012) (2012) (2012)",
            "Mystic River (2004) (2004)"):
        n1 = parse_media_name(stuttered + ".mkv")
        clean = movie_folder(n1)
        assert clean is not None
        # round-trip: reparse the clean folder + ext -> same folder
        n2 = parse_media_name(clean + ".mkv")
        assert movie_folder(n2) == clean


def test_movie_folder_preserves_legit_double_year_titles():
    """A title like '(1984) Director's Cut (2010)' has two distinct years —
    stutter detector sees repetition but _dedup_year only strips the movie year.
    Route→self keeps it stable in reorganize."""
    n = MediaName("Foo (1984) Directors Cut", 2010, None, None)
    assert movie_folder(n) == "Foo (1984) Directors Cut (2010)"
