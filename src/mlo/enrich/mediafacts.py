"""Media metadata producers for the review-set (P21/B3).

The injected-map pattern `hints.doc_props_map` already established: read
once, hand critics the answer as a plain dict, never re-derive per item.
These feed `seam.build_review_set`'s `media_tags=`/`title_candidates=`
params, which `agent/critics.py` already renders into the critic prompt —
closing the gap where critic prompts asked for "ID3 tags" / "TMDb evidence"
that the pipeline never actually supplied (the "ghost evidence" the review
found: prompts promised signals no producer ever attached).

Pure orchestration over existing connectors (id3.read_tags, tmdb.search_movie,
evidence.clean_name) — no new I/O primitives, no filesystem writes.
"""
from __future__ import annotations

import os

from . import id3, tmdb
from .evidence import clean_name


def tags_map(root: str, rows) -> dict[str, dict]:
    """relpath -> id3.read_tags() for every row, keyed by the exact relpath
    string each row carries. Only present, non-empty tag reads are included
    (id3.read_tags degrades to None without mutagen, on an unreadable file,
    or a file with no tags) — absent from the map, never a null entry, so
    callers can plain dict.get()."""
    out: dict[str, dict] = {}
    for r in rows:
        rel = r["relpath"] if isinstance(r, dict) else r
        tags = id3.read_tags(os.path.join(root, rel))
        if tags:
            out[rel] = tags
    return out


def movie_candidates(items: list[dict], *, key: str | None = None,
                     transport=None) -> dict[str, dict]:
    """relpath -> best TMDb match for every Video-bucket review item; the
    QUERY uses the cleaned filename title (enrich.evidence.clean_name — the
    same scene-tag/site-junk stripping the web-search query composer uses),
    memoized per distinct title so CD1/CD2-style siblings cost one call.
    Absent for items with no match, no key, or a network failure — never a
    null entry. A missing/unreachable TMDb never blocks the caller."""
    out: dict[str, dict] = {}
    by_title: dict[str, dict | None] = {}
    for it in items:
        if it.get("bucket") not in ("Video", "Videos"):
            continue
        title = clean_name(it["relpath"])
        if not title:
            continue
        if title not in by_title:
            by_title[title] = tmdb.search_movie(title, key=key,
                                                transport=transport)
        if by_title[title]:
            out[it["relpath"]] = by_title[title]
    return out
