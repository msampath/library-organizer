"""Content fingerprints. Pure functions; no store, no policy.

quick(path)  -> (size, quick_hash) where quick_hash = SHA-256 over the first
                128 KiB plus (when the file is larger) the last 128 KiB.
                Cheap enough for hundreds of thousands of files on spinning
                disks; strong enough for staging decisions because staging is
                reversible and disposal is human (architecture §13).
full(path)   -> SHA-256 of the whole file, for escalation (near-matches,
                copy verification sampling).
"""
from __future__ import annotations

import hashlib
import os

from . import winpath

CHUNK = 128 * 1024
_SUBCHUNK = 4 * 1024 * 1024      # bounded read size regardless of `chunk` (P21/A1)


def _hash_span(f, h, n: int) -> None:
    """Hash exactly the next `n` bytes from f's current position, in bounded
    _SUBCHUNK reads — a large `chunk` (an escalated destructive-adjacent
    confirm) never allocates more than one sub-chunk at a time."""
    remaining = n
    while remaining > 0:
        block = f.read(min(_SUBCHUNK, remaining))
        if not block:
            break
        h.update(block)
        remaining -= len(block)


def region(path: str, chunk: int = CHUNK) -> tuple[int, str]:
    """(size, SHA-256 over the first `chunk` bytes + the last `chunk`).

    quick() is region(path, 128 KiB). A larger chunk is the sanctioned
    escalation when a decision is destructive (docs: fingerprint sufficiency):
    1 MiB head+tail makes a same-size / same-ends / different-middle collision
    vanishingly unlikely without paying for a full-file read."""
    lp = winpath.to_long(path)
    size = os.path.getsize(lp)
    h = hashlib.sha256()
    with open(lp, "rb") as f:
        _hash_span(f, h, min(chunk, size))
        if size > chunk:
            f.seek(max(0, size - chunk))
            _hash_span(f, h, chunk)
    return size, h.hexdigest()


def quick(path: str) -> tuple[int, str]:
    return region(path, CHUNK)


def full(path: str) -> str:
    h = hashlib.sha256()
    with open(winpath.to_long(path), "rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def confirm_duplicate(a: str, b: str, *, full_threshold: int = 256 * 1024) -> bool:
    """True iff `a` and `b` are genuine staging-out duplicates: equal size,
    equal quick (128 KiB head+tail) hash, and — above `full_threshold` — an
    equal FULL SHA-256. Below the threshold the head+tail regions already
    cover the whole file, so quick() alone is already a full-content
    comparison; escalating there would just re-read the same bytes. This is
    the fixed policy every staging-out decision uses (docs: [[fingerprint-
    sufficiency]], P21/A3) — never a same-size/same-ends/different-middle
    false positive, regardless of file size. Total: an unreadable side is
    'not confirmed', never an exception."""
    try:
        qa, qb = quick(a), quick(b)
    except OSError:
        return False
    if qa != qb:
        return False
    if qa[0] <= full_threshold:
        return True
    try:
        return full(a) == full(b)
    except OSError:
        return False


def confirm_same(a: str, b: str, chunk: int = CHUNK) -> bool:
    """True iff `a` and `b` are byte-identical at the `chunk` tier (equal size
    AND equal head+tail hash). This is the check a skill or critic MUST pass
    before it calls one file redundant of another: the quick screen only
    nominates, region() confirms, and a destructive-adjacent caller raises
    `chunk` to 1 MiB (§8, [[fingerprint-sufficiency]]). Total — an unreadable
    side is "not confirmed", never an exception."""
    try:
        return region(a, chunk) == region(b, chunk)
    except OSError:
        return False
