"""Fuzzy title matching — stdlib by default, pure, total.

The skill the transliterated-title critics (§4.1) lean on: match a query title
against a set of candidates when spelling varies (transliteration, disc/part
suffixes, punctuation, WORD ORDER, or unparsed release-scene junk), and
ABSTAIN when the match is not clear enough for a machine to decide.

The ladder — cheapest, most reliable signal first (the verification-economics
posture of §8, applied to strings):

  1. normalize   — casefold, drop parenthesized years, strip disc/part tokens
                   (CD1, Disc 2, Part 3 — pattern-driven, not a hardcoded list),
                   fold punctuation to spaces. An exact normalized match is
                   distance 0.
  2. token match (P21/B4) — word-ORDER-insensitive: 'Rings, The Lord of the'
                   vs 'The Lord of the Rings' share every word but Damerau-
                   Levenshtein (a character-POSITION metric) never sees it.
                   `thefuzz` (the `enrich` extra) supplies token_sort/
                   token_set ratios when installed — token_set additionally
                   tolerates extra/missing words (release-scene junk like
                   '[1080p] [YIFY]'); a pure-stdlib sorted-token-set equality
                   check is the fallback when `thefuzz` isn't installed (exact
                   reordering only — no partial credit without a real
                   similarity metric).
  3. Damerau-Levenshtein — edit distance with adjacent transposition, the
                   dominant transliteration error. The primary character-level
                   signal.
  4. Soundex     — a GUARDED phonetic fallback: accepted only for a candidate
                   that both sounds alike AND sits within threshold+slack edits,
                   and only when it is the unique such candidate. Never on its own.

Abstention is a first-class result. A query with no close candidate, or a TIE
between two equally-good candidates (the 'Dostana (1980)' vs 'Dostana (2008)'
problem — the title alone cannot decide), returns None; the caller escalates to
the human. best_match never guesses a winner it cannot defend.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

try:
    from thefuzz import fuzz as _thefuzz          # optional: the `enrich` extra
except ImportError:                               # pragma: no cover (env-dependent)
    _thefuzz = None

# Disc/part indicators, pattern-driven so a critic can extend them from config
# rather than the engine carrying a hardcoded CD1/CD2 list (canonical §5.1).
# Each matches the marker glued or separated: 'BluffmasterCD1', 'Film.Disc 2'.
DEFAULT_PART_PATTERNS: tuple[str, ...] = (
    r"(?:cd|disc|disk|part)[\s._-]*\d{1,2}\b",
)

_PAREN_YEAR = re.compile(r"\((?:19|20)\d{2}\)")
_NONWORD = re.compile(r"[\W_]+", re.UNICODE)


@dataclass(frozen=True)
class Match:
    candidate: str          # the winning candidate, verbatim as given
    distance: int           # edit distance to the normalized query (0 for token)
    method: str             # 'exact' | 'token' | 'edit' | 'soundex'
    confidence: float       # 0..1; soundex matches are deliberately low


def normalize(s: str, part_patterns: tuple[str, ...] = DEFAULT_PART_PATTERNS) -> str:
    """Comparison form: casefold, drop parenthesized years and disc/part tokens,
    fold punctuation/underscores to single spaces. Total."""
    s = s.casefold()
    s = _PAREN_YEAR.sub(" ", s)
    for pat in part_patterns:
        s = re.sub(pat, " ", s, flags=re.IGNORECASE)
    return _NONWORD.sub(" ", s).strip()


def damerau_levenshtein(a: str, b: str) -> int:
    """Optimal string alignment distance (edits + adjacent transpositions)."""
    la, lb = len(a), len(b)
    if not la:
        return lb
    if not lb:
        return la
    prev2: list[int] = []
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if (i > 1 and j > 1 and a[i - 1] == b[j - 2]
                    and a[i - 2] == b[j - 1]):
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[lb]


_SOUNDEX_CODES = {
    **dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"),
    **dict.fromkeys("dt", "3"), "l": "4", **dict.fromkeys("mn", "5"), "r": "6",
}


def soundex(s: str) -> str:
    """Classic 4-char Soundex code (letter + 3 digits), or '' for no letters."""
    letters = [c for c in s.casefold() if c.isalpha() and c.isascii()]
    if not letters:
        return ""
    out = letters[0].upper()
    prev = _SOUNDEX_CODES.get(letters[0], "")
    for c in letters[1:]:
        code = _SOUNDEX_CODES.get(c, "")
        if code and code != prev:
            out += code
            if len(out) == 4:
                break
        # h and w do not reset the "previous code" adjacency rule; vowels do
        if c not in "hw":
            prev = code
    return (out + "000")[:4]


_TOKEN_RATIO_THRESHOLD = 92.0


def _token_ratio(a: str, b: str) -> float:
    """0..100 word-order-insensitive similarity between two ALREADY-normalized
    strings. `thefuzz`'s token_sort/token_set ratios (max of the two —
    token_set additionally tolerates extra/missing words, e.g. unparsed
    release-scene junk) when installed; a pure-stdlib sorted-token-set
    equality check otherwise (100 for the same set of words in any order,
    else 0 — no partial credit without a real similarity metric)."""
    if _thefuzz is not None:
        return max(_thefuzz.token_sort_ratio(a, b),
                   _thefuzz.token_set_ratio(a, b))
    return 100.0 if sorted(a.split()) == sorted(b.split()) else 0.0


def _token_tier(nq: str, candidates: list[str],
                part_patterns: tuple[str, ...]) -> Match | None:
    """The word-order-insensitive tier (P21/B4): scores every candidate,
    accepts only a UNIQUE best at/above _TOKEN_RATIO_THRESHOLD — same
    tie-abstains posture as every other tier."""
    scored: list[tuple[float, str]] = []
    for cand in candidates:
        nc = normalize(cand, part_patterns)
        if not nc:
            continue
        r = _token_ratio(nq, nc)
        if r >= _TOKEN_RATIO_THRESHOLD:
            scored.append((r, cand))
    if not scored:
        return None
    scored.sort(key=lambda t: -t[0])
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None                              # tie -> abstain
    best_r, best_cand = scored[0]
    return Match(best_cand, 0, "token", round(best_r / 100, 3))


def best_match(query: str, candidates: list[str], *,
               part_patterns: tuple[str, ...] = DEFAULT_PART_PATTERNS,
               max_ratio: float = 0.34, soundex_slack: int = 2) -> Match | None:
    """The best defensible candidate for `query`, or None (abstain).

    Abstains when there is no candidate within an edit threshold (scaled to the
    query length) and no unique phonetic fallback, OR when two candidates tie
    for best — the caller sends a tie to the human, never coin-flips it."""
    nq = normalize(query, part_patterns)
    if not nq or not candidates:
        return None
    scored: list[tuple[int, str]] = []
    for cand in candidates:
        nc = normalize(cand, part_patterns)
        if nc:
            scored.append((damerau_levenshtein(nq, nc), cand))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1]))
    best_d, best_cand = scored[0]
    if len(scored) > 1 and scored[1][0] == best_d:
        return None                              # tie -> ambiguous -> abstain

    threshold = max(1, int(len(nq) * max_ratio))
    if best_d == 0:
        return Match(best_cand, 0, "exact", 1.0)

    # P21/B4: word-order-insensitive tier, tried before the edit-distance
    # accept/reject — 'Rings, The Lord of the' vs 'The Lord of the Rings'
    # share every word but Damerau-Levenshtein (a character-POSITION metric)
    # never sees it. Never reached for an already-exact match (returned
    # above), so it only ever ADDS acceptances, never overrides one.
    token_hit = _token_tier(nq, candidates, part_patterns)
    if token_hit is not None:
        return token_hit

    if best_d <= threshold:
        return Match(best_cand, best_d, "edit",
                     round(1.0 - best_d / (len(nq) + 1), 3))

    # Guarded Soundex fallback: a candidate that sounds like the query and is
    # within threshold+slack edits — accepted only if it is the unique one.
    sq = soundex(nq)
    phon = [(d, c) for (d, c) in scored
            if soundex(normalize(c, part_patterns)) == sq
            and d <= threshold + soundex_slack]
    if sq and len(phon) == 1:
        d, c = phon[0]
        return Match(c, d, "soundex", round(max(0.3, 0.6 - 0.05 * d), 3))
    return None
