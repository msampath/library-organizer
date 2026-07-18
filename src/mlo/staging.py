"""Staging-root resolution (P21/A4 — the B1 blocker).

Before this module, `[staging]` could only be keyed by a single Windows drive
letter (config.py). `winpath.drive_of()` already returns a UNC share as one
atomic string ('\\\\SERVER\\SHARE'), so a UNC key already resolves correctly
via a plain dict lookup once config.py stops rejecting it — the real gap is
POSIX, where `drive_of()` always returns '' (there is no drive-letter concept
there at all). This module adds a second key form — an absolute path prefix
('/mnt/media', or a UNC share written the same way) — resolved by
longest-prefix match, which is the only signal available on POSIX; on
Windows, prefix keys apply to UNC paths (a drive-letter path key such as
"E:/sub" is not an accepted key shape — config.py refuses it).

Pure; no filesystem mutation. `same_volume`'s POSIX branch does read-only
`os.stat` calls (never on a not-yet-created destination — it walks up to the
nearest EXISTING ancestor first).
"""
from __future__ import annotations

import os

from . import winpath


def is_drive_letter_key(key: str) -> bool:
    return len(key) == 1 and key.isalpha()


def is_staging_prefix_key(key: str) -> bool:
    """A UNC share ('\\\\server\\share') or a POSIX absolute mount path
    ('/mnt/media') — an OS-agnostic string shape, checked structurally so the
    same validation logic applies however mlo.toml was authored."""
    return key.startswith("\\\\") or key.startswith("//") or key.startswith("/")


def root_for(staging: dict[str, str], path: str, drive_of=None) -> str | None:
    """The staging root governing `path`, or None if none is configured.

    Two key forms are tried: (1) drive_of(path) as an EXACT key — a single
    Windows drive letter, or the whole UNC share prefix drive_of already
    returns as one atomic string; (2) failing that, LONGEST-PREFIX match
    against every absolute-path-shaped key (UNC or POSIX) — the only signal
    on POSIX."""
    drive_of = drive_of or winpath.drive_of
    drive = drive_of(path)
    if drive and drive in staging:
        return staging[drive]
    best_root, best_len = None, -1
    for key, root in staging.items():
        if is_drive_letter_key(key):
            continue
        if winpath.is_under(path, key) and len(key) > best_len:
            best_root, best_len = root, len(key)
    return best_root


def _nearest_existing_ancestor(path: str) -> str:
    p = os.path.abspath(winpath.from_long(path))
    while p and not os.path.exists(winpath.to_long(p)):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def same_volume(a: str, b: str, drive_of=None) -> bool:
    """True iff `a` and `b` are on the same physical volume, for the
    stage_move same-drive placement rule. Windows/UNC: drive_of(...) identity
    (unchanged behavior). POSIX (drive_of always ''): st_dev of each path's
    nearest EXISTING ancestor — a not-yet-created destination is never
    stat'd directly. An unreadable/unresolvable pair defaults to True (today's
    POSIX behavior: the rule never blocks when it cannot prove a difference;
    the real cross-device guard is safeops._rename_no_overwrite's st_dev
    check at execute time)."""
    drive_of = drive_of or winpath.drive_of
    da, db = drive_of(a), drive_of(b)
    if da or db:
        return da == db
    try:
        sa = os.stat(winpath.to_long(_nearest_existing_ancestor(a))).st_dev
        sb = os.stat(winpath.to_long(_nearest_existing_ancestor(b))).st_dev
        return sa == sb
    except OSError:
        return True
