"""fuzzy — title matching with disc/part stripping, edit distance, guarded
Soundex, and abstention on genuine ambiguity."""
from __future__ import annotations

from hypothesis import given, settings, strategies as st

from mlo import fuzzy


# ── normalize: strip disc/part tokens and parenthesized years ────────────────

def test_normalize_strips_part_tokens_glued_and_separated():
    assert fuzzy.normalize("BluffmasterCD1") == "bluffmaster"
    assert fuzzy.normalize("Bluffmaster.CD 2") == "bluffmaster"
    assert fuzzy.normalize("The Film - Disc 3") == "the film"
    assert fuzzy.normalize("Movie Part2") == "movie"


def test_normalize_strips_parenthesized_year_but_not_title_numbers():
    assert fuzzy.normalize("Bluffmaster (2005)") == "bluffmaster"
    # a leading bare year is part of the title (naming.py's posture)
    assert fuzzy.normalize("2001 A Space Odyssey (1968)") == "2001 a space odyssey"


# ── best_match: the three acceptance cases ───────────────────────────────────

def test_disc_suffix_matches_the_bare_title():
    """'BluffmasterCD1' resolves to the library's 'Bluffmaster (2005)' — the
    disc marker and the year are both normalized away, leaving an exact match."""
    m = fuzzy.best_match("BluffmasterCD1",
                         ["Bluffmaster (2005)", "Om Shanti Om (2007)"])
    assert m is not None
    assert m.candidate == "Bluffmaster (2005)"
    assert m.method == "exact" and m.confidence == 1.0


def test_transliterated_title_fuzzy_matches():
    """A transliteration variant (an extra vowel) matches by edit distance."""
    m = fuzzy.best_match("Ajab Prem Ki Ghazab Kahaani",
                         ["Ajab Prem Ki Ghazab Kahani (2009)", "Wanted (2009)"])
    assert m is not None
    assert m.candidate == "Ajab Prem Ki Ghazab Kahani (2009)"
    assert m.method == "edit" and m.distance == 1 and m.confidence > 0.9


def test_ambiguous_title_abstains_to_human():
    """'Dostana' matches two library entries equally (1980 and 2008): the title
    alone cannot decide, so the matcher abstains rather than coin-flip."""
    assert fuzzy.best_match("Dostana",
                            ["Dostana (1980)", "Dostana (2008)"]) is None


def test_no_close_candidate_abstains():
    assert fuzzy.best_match("Inception", ["Roja (1992)", "Sholay (1975)"]) is None


def test_empty_query_or_candidates_abstains():
    assert fuzzy.best_match("", ["Roja (1992)"]) is None
    assert fuzzy.best_match("Roja", []) is None
    assert fuzzy.best_match("!!!", ["Roja (1992)"]) is None


# ── token tier: word-order-insensitive (P21/B4) ──────────────────────────────

def test_reordered_words_match_via_token_tier():
    """'Rings, The Lord of the' vs 'The Lord of the Rings' share every word
    but Damerau-Levenshtein (character-position) would never see it — the
    token tier catches the reorder."""
    m = fuzzy.best_match("Rings, The Lord of the",
                         ["The Lord of the Rings (2001)", "The Two Towers (2002)"])
    assert m is not None
    assert m.candidate == "The Lord of the Rings (2001)"
    assert m.method == "token" and m.confidence >= 0.9


def test_token_tier_does_not_override_an_exact_match():
    """A genuinely exact normalized match still wins the 'exact' tier — the
    token tier is only ever reached when best_d != 0."""
    m = fuzzy.best_match("Bluffmaster",
                         ["Bluffmaster (2005)", "retsamffulB"])  # reversed junk
    assert m is not None
    assert m.candidate == "Bluffmaster (2005)" and m.method == "exact"


def test_token_tier_abstains_on_tie():
    """Two DIFFERENT candidates share the exact same reordered word set as
    the query (neither matches the query's own word order, so neither is an
    'exact' match): ambiguous, abstain — same posture as every other tier."""
    m = fuzzy.best_match("Ek Do Teen", ["Teen Ek Do", "Do Teen Ek"])
    assert m is None


def test_token_tier_stdlib_fallback_requires_exact_reordering(monkeypatch):
    monkeypatch.setattr(fuzzy, "_thefuzz", None)
    """Without `thefuzz` installed, the token tier accepts ONLY an exact
    reordered word-set match — no partial credit for a near-miss (that stays
    the edit-distance tier's job)."""
    # forced stdlib branch — must hold whether or not thefuzz is installed
    # (the enrich extra ships it; CI installs only [dev]) — super-review B-058
    # 'Twin' vs 'Two' — one word actually differs, not just reordered, so the
    # token tier must NOT claim it.
    m = fuzzy.best_match("The Two Towers Reordered Now",
                         ["Now Reordered Towers The Twin"])
    assert m is None or m.method != "token"


def test_token_ratio_thefuzz_absent_uses_stdlib_binary_fallback(monkeypatch):
    monkeypatch.setattr(fuzzy, "_thefuzz", None)
    assert fuzzy._token_ratio("a b c", "c b a") == 100.0
    assert fuzzy._token_ratio("a b c", "a b d") == 0.0


def test_token_ratio_uses_thefuzz_when_installed(monkeypatch):
    """The installed branch, exercised via a fake thefuzz module so the
    test is hermetic either way (super-review B-058)."""
    class _FakeFuzz:
        @staticmethod
        def token_sort_ratio(a, b):
            return 83
        @staticmethod
        def token_set_ratio(a, b):
            return 91
    monkeypatch.setattr(fuzzy, "_thefuzz", _FakeFuzz)
    assert fuzzy._token_ratio("x", "y") == 91.0


# ── guarded Soundex fallback ─────────────────────────────────────────────────

def test_soundex_is_a_guarded_second_level_fallback():
    """Beyond the edit threshold, a UNIQUE phonetic match is accepted (with a
    deliberately low confidence). A strict ratio forces the edit tier to miss so
    the Soundex tier is exercised."""
    m = fuzzy.best_match("Katharyne", ["Katherine"], max_ratio=0.05)
    assert m is not None
    assert m.candidate == "Katherine" and m.method == "soundex"
    assert m.confidence < 0.7          # reviewable, not trusted like an exact


# ── the algorithms, unit-checked ─────────────────────────────────────────────

def test_damerau_levenshtein_known_distances():
    assert fuzzy.damerau_levenshtein("ab", "ba") == 1          # transposition
    assert fuzzy.damerau_levenshtein("kitten", "sitting") == 3
    assert fuzzy.damerau_levenshtein("abc", "abc") == 0
    assert fuzzy.damerau_levenshtein("", "abc") == 3
    assert fuzzy.damerau_levenshtein("abc", "") == 3


def test_soundex_known_codes():
    assert fuzzy.soundex("Robert") == "R163"
    assert fuzzy.soundex("Rupert") == "R163"
    assert fuzzy.soundex("Tymczak") == "T522"
    assert fuzzy.soundex("") == ""
    assert fuzzy.soundex("12345") == ""


# ── totality / invariants ────────────────────────────────────────────────────

@settings(max_examples=200, deadline=None)
@given(st.text(max_size=30), st.lists(st.text(max_size=30), max_size=6))
def test_best_match_is_total(query, candidates):
    out = fuzzy.best_match(query, candidates)
    assert out is None or (isinstance(out, fuzzy.Match)
                           and out.candidate in candidates
                           and 0.0 <= out.confidence <= 1.0)


@settings(max_examples=100, deadline=None)
@given(st.text(max_size=20), st.text(max_size=20))
def test_damerau_is_symmetric(a, b):
    assert fuzzy.damerau_levenshtein(a, b) == fuzzy.damerau_levenshtein(b, a)


@settings(max_examples=100, deadline=None)
@given(st.text(max_size=40))
def test_normalize_is_idempotent(s):
    once = fuzzy.normalize(s)
    assert fuzzy.normalize(once) == once
