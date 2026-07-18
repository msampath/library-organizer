r"""Deterministic evidence assembly for the critic — the token-efficient path.

The critic must NEVER web-search as an agentic tool: N turns re-send the whole
context N times. Instead Python does the legwork with ZERO model tokens —
extract the file's FACTS, COMPOSE a search query from them, run the search
BATCHED and CACHED — and hands the critic ONE bounded prompt carrying
{facts, siblings, results}. An accepted judgment is then distilled to a rule so
the same case never searches (or calls the model) again.

  file_facts(item)     -> the facts a critic needs (kind, ext, title, siblings)
  compose_query(item)  -> the web-search string, or None (numeric carve)
  assemble(items, ...) -> dedup + batch + cache the searches, attach results to
                          each item['evidence']

This module is pure/deterministic apart from the injected search callable; the
search itself is enrich.websearch (batched, offline-safe, transport-injected).
"""
from __future__ import annotations

import os
import re

from .websearch import batch_queries

# Kind -> the disambiguating noun that steers a search at the right medium.
_KIND_TERM = {"Audio": "song", "Video": "movie", "Videos": "movie"}
_URL_ESC = re.compile(r"%([0-9A-Fa-f]{2})")
_BRACKET = re.compile(r"\[[^\]]*\]")
# Download-site pollution: a www.* domain, a (Site.com) tag, or a known ripper.
_SITE = re.compile(
    r"[\s._-]*(?:www\.\S+|\([^)]*\.(?:com|net|org|in|pk)[^)]*\)|"
    r"(?:download\s?ming|songs?\.?pk|pagal\s?world|wap\s?king|dj\s?maza|"
    r"keep-?mp3|mass\s?tamilan|star\s?musiq|isaimini)\S*)", re.IGNORECASE)
_PART = re.compile(r"\b(cd|disc|disk|part)[\s._-]*\d+\b", re.IGNORECASE)
_ATTRIB = re.compile(r"\s*--\s*.+$")                     # ' -- Tulsidas' (bhajan poet)
# Leading track number: '01.', '01 - ', or a zero-/2-3-digit '01 ' — but NOT a
# bare single digit, which may be a real title ('3', the 2012 Tamil film).
_TRACK = re.compile(r"^\s*(?:\d{1,3}\s*[.\-)]\s*|\d{2,3}\s+)")


def clean_name(relpath: str) -> str:
    """A human title from the filename stem: URL-decode (`%20`->space), strip
    scene tags, download-site pollution, a `-- Author` attribution, disc markers
    and a leading track number, and normalize separators."""
    base = os.path.splitext(relpath.replace("\\", "/").rsplit("/", 1)[-1])[0]
    base = _URL_ESC.sub(lambda m: chr(int(m.group(1), 16)), base)
    base = _BRACKET.sub(" ", base)
    base = _SITE.sub(" ", base)
    base = _ATTRIB.sub("", base)
    base = _PART.sub(" ", base)
    base = _TRACK.sub("", base)
    base = re.sub(r"[._]+", " ", base)
    return re.sub(r"\s+", " ", base).strip(" -")


def file_facts(item: dict) -> dict:
    """The deterministic facts a critic needs — assembled with zero model tokens."""
    return {
        "kind": item.get("bucket"),
        "ext": item.get("ext"),
        "title": clean_name(item["relpath"]),
        "language_guess": item.get("language_guess"),
        "siblings": item.get("siblings", []),
    }


def compose_query(item: dict, cfg) -> str | None:
    """Build the web-search string from the file's facts. None when the file
    should not be searched: a numeric/hex carve, or — for audio — anything the
    pre-classifier says is not a song (a WhatsApp voice note, a discourse, junk
    all route deterministically with zero model tokens)."""
    f = file_facts(item)
    if f["kind"] == "Audio":
        from .. import audioclass
        base = item["relpath"].replace("\\", "/").rsplit("/", 1)[-1]
        # Same call shape as the router (taxonomy.route_audio): the user's
        # [classify.audio_patterns] conventions apply here too — a file the
        # config declares junk/comedy must not compose a search query.
        if audioclass.classify(
                base, getattr(cfg, "audio_patterns", None) or None) != "song":
            return None
    title = f["title"]
    if len(re.sub(r"[^A-Za-z]", "", title)) < 3:          # needs real letters
        return None
    terms = [f'"{title}"']
    lang = f["language_guess"]
    if lang and lang.casefold() != cfg.layout.default_language.casefold():
        terms.append(lang.lower())
    term = _KIND_TERM.get(f["kind"])
    if term:
        terms.append(term)
    return " ".join(terms)


def assemble(items: list[dict], cfg, *, search_fn=None,
             cache: dict | None = None) -> dict:
    """Compose a query per item, DEDUP identical queries, run them BATCHED
    through `search_fn` (a callable taking a list of queries -> {query: results}),
    CACHE by query, and attach {web_query, web} to each item['evidence'].

    Returns a rollup {items, queries, unique_queries, searched, cache_hits} so
    the token/lookup cost is auditable. With no search_fn the queries are still
    composed and cached-check'd (the offline path)."""
    cache = {} if cache is None else cache
    by_query: dict[str, list[dict]] = {}
    cache_hits = 0
    cached_queries: set[str] = set()
    for it in items:
        q = compose_query(it, cfg)
        if not q:
            continue
        ev = it.setdefault("evidence", {})
        ev["web_query"] = q
        if q in cache:
            ev["web"] = cache[q]
            cache_hits += 1
            cached_queries.add(q)
        else:
            by_query.setdefault(q, []).append(it)

    searched = 0
    if by_query and search_fn is not None:
        for group in batch_queries(list(by_query)):
            results = search_fn(group)
            for q in group:
                cache[q] = results.get(q, [])
                for it in by_query[q]:
                    it.setdefault("evidence", {})["web"] = cache[q]
                searched += 1

    composed = sum(1 for it in items
                   if "web_query" in it.get("evidence", {}))
    return {"items": len(items), "queries": composed,
            # distinct queries this call dealt with: newly searched groups
            # plus DISTINCT cache-served queries (cache_hits counts items,
            # which inflated this rollup on cache-heavy runs — A-61)
            "unique_queries": len(by_query) + len(cached_queries),
            "searched": searched, "cache_hits": cache_hits}
