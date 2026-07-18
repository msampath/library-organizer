r"""Full-path metadata extraction — the file-organizer's core signal.

Everything derives from the FULL PATH, not just the filename: the folder
hierarchy is metadata as good as EXIF. A DVD rip's filename (`VTS_10_1.VOB`) is
structural junk, but its path — `…\Movies\Tamil\KANDUKONDAEN KANDUKONDAEN\` —
spells out the type, language and title. Sources named things many ways
(UPPERCASE `MOVIES\ENGLISH`, junk grouping folders `!_watched_!`, actor folders
`RAJINI\Siva`, deep provenance prefixes `BeaTB\FINAL\`), so this classifies the
path with a LIBRARY of pattern families, never one brittle regex.

`derive(cfg, relpath) -> PathMeta`. Pure and total. Language is intentionally
NOT computed here — taxonomy.detect_language already owns path-based language
detection and the router applies it; pathmeta only recovers type/title/year that
the filename could not.

Scope of this cut: MOVIE identity from a `Movies` type-segment (the Videos\<src>\
and Movies\ dumps). TV/anime/music/photo path-mining are follow-ups; a path with
no recognized movie type-segment yields an empty PathMeta and the router falls
through unchanged.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from . import naming

# Type keywords, matched case-insensitively per path segment. Kept to the
# conventions seen in the real library; extensible via config later (L6).
_MOVIE_TYPES = frozenset({"movies", "movie"})

# Folders that are DVD/disc structure, never a title — walked over when picking
# the title.
_STRUCTURE = frozenset({"video_ts", "audio_ts", "bdmv", "stream"})

# Deliberate holding pens: a file UNDER one was intentionally left unidentified,
# so path-derivation must not re-home it (it stays for a critic).
_HOLDING_PEN = frozenset({"unclassified", "unsorted"})

# Placeholder folder names that are not titles but do not imply "held" — skipped
# when picking the title. ('Other' is also the default-language folder.)
_NON_TITLE = frozenset({"other", "misc", "miscellaneous", "unknown", "various",
                        "new folder", "temp", "tmp"})

_TAG = re.compile(r"\[[^\]]*\]")             # scene/quality tags: [a4e], [da-anime.info]


@dataclass(frozen=True)
class PathMeta:
    media_type: str | None = None            # 'movie' | None (this cut)
    title: str | None = None
    year: int | None = None


def _language_names(cfg) -> set[str]:
    lay = cfg.layout
    return {n.casefold() for n in lay.languages} | {lay.default_language.casefold()}


def derive(cfg, relpath: str) -> PathMeta:
    """Recover (media_type, title, year) from the FULL path. Empty PathMeta when
    the path carries no recognizable movie identity."""
    parts = relpath.replace(os.sep, "/").split("/")
    folders = parts[:-1]

    # A movie type-segment anywhere in the path gates movie treatment (the last
    # one wins if repeated). Without it, this is not a path-derivable movie.
    type_idx = None
    for i, seg in enumerate(folders):
        if seg.casefold() in _MOVIE_TYPES:
            type_idx = i
    if type_idx is None:
        return PathMeta()

    below = folders[type_idx + 1:]
    if any(seg.casefold() in _HOLDING_PEN for seg in below):
        return PathMeta()      # under a deliberate holding pen -> leave it there

    # Title = the deepest meaningful folder BELOW the type-segment: the file's
    # own parent in every real DVD case (KANDUKONDAEN, BASIC INSTINCT, Siva),
    # skipping DVD structure folders and language folders. Junk/actor folders
    # (!_watched_!, RAJINI) sit ABOVE the title, so this never picks them.
    lang_names = _language_names(cfg)
    title = None
    for seg in reversed(below):
        low = seg.casefold()
        if low in _STRUCTURE or low in lang_names or low in _NON_TITLE:
            continue
        cleaned = naming.clean_title(_TAG.sub(" ", seg))
        if not cleaned:
            continue
        # A real title has letters, OR is a parenthesized year ('1408 (2007)').
        # A bare number placeholder ('(1)', '(100)') is neither -> skip it.
        if len(re.sub(r"[^A-Za-z]", "", cleaned)) >= 2 or naming.parse_year(cleaned):
            title = cleaned
            break
    if not title:
        return PathMeta()

    return PathMeta(media_type="movie", title=title, year=naming.parse_year(title))
