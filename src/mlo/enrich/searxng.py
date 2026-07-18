"""SearXNG search adapter (P21/B1) — the `--live-search` transport for
`enrich.evidence.assemble`. Self-hosted, keyless (owner decision): the caller
points `[enrich] searxng_url` at their own instance; there is no bundled
public endpoint and no API key. Same connector posture as every module in
this package: fetch/parse only, never writes a file, degrades to a clean
empty result offline or on any network failure — a connector never breaks
the deterministic core.
"""
from __future__ import annotations

import urllib.parse

from . import NETWORK_ERRORS, get_json


def search_fn(searxng_url: str, timeout: int = 20):
    """An `evidence.assemble(search_fn=...)`-shaped callable bound to one
    SearXNG instance: `search_fn(queries) -> {query: [hit, ...]}` — one
    positional arg (the group), a dict keyed by query string (evidence.py's
    contract: `results.get(q, [])` per query in the group). One HTTP request
    per query (SearXNG has no native multi-query batch endpoint); a single
    query's failure yields [] for that query only, never fatal to the group."""
    base = searxng_url.rstrip("/")

    def _call(queries: list[str]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for q in queries:
            url = base + "/search?" + urllib.parse.urlencode(
                {"q": q, "format": "json"})
            try:
                data = get_json(url, {}, timeout)
                if not isinstance(data, dict):
                    raise ValueError("non-object JSON body")
                results = [r for r in (data.get("results") or [])[:5]
                           if isinstance(r, dict)]
                # shaping stays INSIDE the try: a malformed body must degrade
                # to [] like any network failure, never crash the pilot
                # (the module's own offline-safe contract; super-review B-056)
                out[q] = [{"title": r.get("title"), "url": r.get("url"),
                           "content": r.get("content")} for r in results]
            except NETWORK_ERRORS + (AttributeError, TypeError):
                out[q] = []
                continue
        return out
    return _call
