"""Media-name parsing and Jellyfin naming — the L3 danger zone, handled strictly.

The predecessor's `\\d+` regex matched the year in `Movie (2007)` as a duplicate
suffix and nearly merged distinct films (ledger L3). Every parser here is
therefore deliberately narrow, property-tested, and total (never raises on any
string):

  parse_year     — ONLY a parenthesized (19xx|20xx) in a plausible range, and
                   only the LAST one in the stem (titles may contain years:
                   "2001 A Space Odyssey (1968)").
  parse_episode  — S01E02 (case-insensitive) primary; 1x02 secondary with hard
                   word boundaries; never inside a (Year) group.
  clean_title    — separator normalization only ('.'/'_' -> space, collapse).
                   NO aggressive junk-token stripping: overzealous cleaners are
                   how tools mangle titles. Release tags after the year vanish
                   by construction (title = the part BEFORE the year).
  movie_folder   — "Title (Year)" — the Jellyfin folder convention.

Idempotence is load-bearing: routing an already-correct path must produce the
same path, so reorganize plans converge to zero rows (property-tested).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

_YEAR = re.compile(r"\((19\d{2}|20[0-2]\d|203[0-5])\)")   # 1900–2035
_EP_SXXEYY = re.compile(r"(?<![A-Za-z0-9])[Ss](\d{1,2})[\s._-]?[Ee](\d{1,3})(?![0-9])")
_EP_NXNN = re.compile(r"(?<![0-9(])(\d{1,2})x(\d{2,3})(?![0-9)])")
_SEPS = re.compile(r"[._]+")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class MediaName:
    title: str
    year: int | None
    season: int | None
    episode: int | None

    @property
    def is_episode(self) -> bool:
        return self.season is not None and self.episode is not None


def parse_year(stem: str) -> int | None:
    """The last parenthesized plausible year in the stem, else None. Bare
    digits are NEVER a year (L3)."""
    hits = _YEAR.findall(stem)
    return int(hits[-1]) if hits else None


def parse_episode(stem: str) -> tuple[int, int] | None:
    """(season, episode) from S01E02-style (preferred) or 1x02-style markers."""
    m = _EP_SXXEYY.search(stem)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _EP_NXNN.search(stem)
    if m:
        season = int(m.group(1))
        if 1 <= season <= 99:
            return season, int(m.group(2))
    return None


def clean_title(raw: str) -> str:
    """Separator normalization only; conservative on purpose."""
    t = _SEPS.sub(" ", raw)
    t = _WS.sub(" ", t).strip(" -–")
    return t.strip()


def parse_media_name(filename: str) -> MediaName:
    """Total: any string in, a MediaName out (fields None when absent)."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    year = parse_year(stem)

    ep_match = _EP_SXXEYY.search(stem) or _EP_NXNN.search(stem)
    ep = parse_episode(stem)
    if ep and ep_match:
        title = clean_title(stem[:ep_match.start()])
        return MediaName(title=title, year=year, season=ep[0], episode=ep[1])

    if year is not None:
        last = None
        for m in _YEAR.finditer(stem):
            last = m
        title = clean_title(stem[:last.start()])
        if not title:                       # "(2019).mkv" — year but no title
            return MediaName(clean_title(stem), None, None, None)
        return MediaName(title=title, year=year, season=None, episode=None)

    return MediaName(title=clean_title(stem), year=None, season=None, episode=None)


def has_year_stutter(seg: str) -> bool:
    """True when a folder/file stem repeats a parenthesized year — '(2012) (2012)',
    'Title (2004) (2004)' — or leads with one — '(2004) Title'. The C26 disease
    seen on the real library; nothing broader (a junk-tagged folder like
    'Title (Year) [1080p BluRay]' is deliberately tolerated by the router)."""
    hits = _YEAR.findall(seg)
    if len(hits) >= 2:
        return True
    return bool(hits and _YEAR.match(seg.strip()))


def _dedup_year(title: str, year: int) -> str:
    """Strip every '(year)' occurrence from title; movie_folder re-appends one."""
    cleaned = re.sub(r"\s*\(" + str(year) + r"\)", "", title).strip(" -–")
    return cleaned or title


def movie_folder(name: MediaName) -> str | None:
    """Jellyfin movie folder 'Title (Year)'; None when either part is missing
    (the router then falls back rather than guessing)."""
    if not name.title or name.year is None:
        return None
    return f"{_dedup_year(name.title, name.year)} ({name.year})"


def season_folder(season: int) -> str:
    return f"Season {season:02d}"
