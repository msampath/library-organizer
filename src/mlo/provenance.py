"""Provenance — where a library file came from, reconstructed from the journal.

The copy manifest of §2.4: a ground-truth signal for placement and for a
correction pass over *already-placed* files. mlo's manifest is its own op
journal — every `copy_in` (source → library) and `move_within` (library →
library) records (dst, src), so a current library path can be traced back to
the original source path it entered from, following internal moves.

Two hard rules from the corpus:
  - **Provenance INFORMS, it never DETERMINES.** This module only READS the
    journal and returns facts/signals; nothing here mutates the store or the
    filesystem. The caller feeds a signal to the router as a Hint, where the
    ordinary plan gates still apply. It is never a licence to build
    `<Bucket>/<SourceDrive>/` dump folders.
  - **Report the boundary honestly.** Files placed by an external pipeline (the
    v5 predecessor) have no journal record; `origin_of` returns None for them
    and `coverage` counts them as untraced — the manifest says what it does NOT
    cover, rather than guessing.
"""
from __future__ import annotations

import os
import re

from . import winpath
from .config import Config
from .store import Store

# Folder-name signals that a file's ORIGIN was a personal capture/app, not a
# published title — the answer to camera/WhatsApp media misfiled as icons.
# Pattern-driven so a critic can extend them from config (general-principles);
# these are conservative defaults.
DEFAULT_PERSONAL_SOURCE_PATTERNS: tuple[str, ...] = (
    r"whatsapp", r"telegram", r"instagram", r"snapchat", r"messenger",
    r"brightwheel", r"\bdcim\b", r"\bcamera\b", r"screen[\s_-]?shots?",
    r"screen[\s_-]?record", r"voice[\s_-]?(?:notes?|memos?|recorder)",
)


def build_origin_map(store: Store) -> dict[str, str]:
    """{normcased dst path -> src path} over every done placement op, journal
    order so the most recent origin wins for a reused destination."""
    m: dict[str, str] = {}
    for dst, src in store.origin_pairs():
        m[os.path.normcase(dst)] = src
    return m


def origin_of(cfg: Config, origin_map: dict[str, str], relpath: str,
              max_hops: int = 32) -> str | None:
    """The original external source path a library file entered from, tracing
    back through internal moves, or None when the journal cannot trace it (a
    coverage boundary — an externally-placed file)."""
    cur = os.path.join(cfg.library_root, relpath)
    src = origin_map.get(os.path.normcase(cur))
    if src is None:
        return None
    seen = {os.path.normcase(cur)}
    for _ in range(max_hops):
        if not winpath.is_under(src, cfg.library_root):
            break                                    # reached an external origin
        key = os.path.normcase(src)
        if key in seen:
            break                                    # cycle guard (never expected)
        seen.add(key)
        nxt = origin_map.get(key)
        if nxt is None:
            # the chain dead-ends INSIDE the library: the journal never saw
            # this file enter from outside, so there is no external origin —
            # returning the internal path would break the docstring contract
            # and let personal-pattern matching fire on a library path
            # (super-review B-068)
            return None
        src = nxt
    return src


def coverage(store: Store, cfg: Config,
             origin_map: dict[str, str] | None = None) -> dict:
    """How much of the library the journal can trace to an origin. Honest
    boundary reporting — the point of §2.4, not a placement decision."""
    om = build_origin_map(store) if origin_map is None else origin_map
    total = traced = 0
    for row in store.index_iter():
        total += 1
        if origin_of(cfg, om, row["relpath"]) is not None:
            traced += 1
    return {"total": total, "traced": traced, "untraced": total - traced,
            "pct": round(100.0 * traced / total, 1) if total else 0.0}


def origin_signal(origin_path: str | None,
                  patterns: tuple[str, ...] = DEFAULT_PERSONAL_SOURCE_PATTERNS
                  ) -> str | None:
    """A media_kind hint derived from an ORIGIN path's folder names, or None.
    Today only 'personal' (a capture-app/camera source) — INFORMS only; the
    caller passes it to the router as a Hint, it never moves a file itself."""
    if not origin_path:
        return None
    low = origin_path.replace("\\", "/").casefold()
    for pat in patterns:
        if re.search(pat, low):
            return "personal"
    return None
