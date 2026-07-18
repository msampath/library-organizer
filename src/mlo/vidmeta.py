"""Minimal MP4/MOV creation-date reader — stdlib only, read-only, TOTAL.

creation_year() returns a video's embedded creation year (the `mvhd` atom's
creation time, seconds since 1904-01-01 UTC) for MP4/MOV/M4V containers, or
None. It NEVER raises: a video without derivable metadata routes to the
name-embedded-date fallback (imgclass.name_year) and then to "stay put" in
the drain builder, never a guessed year (mtime lies after a copy — C19).

Hard limits, all deliberate (the exif.py precedent):
  - only the first 1 MiB is read; the atom walk never seeks past it — a
    faststart-off MP4 whose `moov` sits at the end simply yields None rather
    than reading the whole file;
  - the atom walk is capped at _MAX_ATOMS siblings per level (no infinite
    loop on a malformed/adversarial size field);
  - years outside 1990-2035 are rejected — same plausibility window as
    exif.year_of, and it rejects the 1904 zero-epoch unset files carry
    (creation_time == 0 decodes to 1904, well outside the window).
"""
from __future__ import annotations

import datetime

from . import winpath

_CAP = 1024 * 1024
_MAX_ATOMS = 4096
_MAC_EPOCH_DELTA = 2082844800   # seconds between 1904-01-01 and 1970-01-01 UTC


def creation_year(path: str) -> int | None:
    try:
        with open(winpath.to_long(path), "rb") as f:
            data = f.read(_CAP)
    except OSError:
        return None
    try:
        return _year_from_bytes(data)
    except Exception:          # total by contract — malformed atoms are "no year"
        return None


def _year_from_bytes(data: bytes) -> int | None:
    moov = _find_atom(data, b"moov")
    if moov is None:
        return None
    mvhd = _find_atom(moov, b"mvhd")
    if mvhd is None:
        return None
    return _year_from_mvhd(mvhd)


def _find_atom(data: bytes, want: bytes) -> bytes | None:
    """Return the payload of the first sibling atom in `data` whose 4-byte
    type equals `want`, else None. Bounded: at most _MAX_ATOMS siblings, and
    never reads past the end of `data` (our 1 MiB cap)."""
    i = 0
    n = len(data)
    count = 0
    while i + 8 <= n and count < _MAX_ATOMS:
        count += 1
        size = int.from_bytes(data[i:i + 4], "big")
        typ = data[i + 4:i + 8]
        header = 8
        if size == 1:                       # 64-bit extended size follows
            if i + 16 > n:
                return None
            size = int.from_bytes(data[i + 8:i + 16], "big")
            header = 16
        if size == 0:                       # atom extends to EOF
            body_end = n
        else:
            if size < header:
                return None                 # malformed — declared size too small
            body_end = min(i + size, n)     # payload may straddle our read cap
        body_start = i + header
        if body_start > body_end:
            return None
        if typ == want:
            return data[body_start:body_end]
        if size == 0:
            return None                     # EOF atom, not a match — nothing follows
        i = body_end
    return None


def _year_from_mvhd(mvhd: bytes) -> int | None:
    if len(mvhd) < 4:
        return None
    version = mvhd[0]
    if version == 0:
        if len(mvhd) < 8:
            return None
        creation = int.from_bytes(mvhd[4:8], "big")
    elif version == 1:
        if len(mvhd) < 12:
            return None
        creation = int.from_bytes(mvhd[4:12], "big")
    else:
        return None
    unix_ts = creation - _MAC_EPOCH_DELTA
    try:
        year = datetime.datetime.fromtimestamp(
            unix_ts, datetime.timezone.utc).year
    except (ValueError, OSError, OverflowError):
        return None
    return year if 1990 <= year <= 2035 else None
