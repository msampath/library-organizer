"""OpenSubtitles connector (P21/B5) — search only. Fetch / parse only; never
writes a file, same posture as every module in this package.

The SubDB predecessor is retired: TheSubDB service shut down (~2020; the
endpoint no longer answers), so it was dead code kept only for its hash
scheme. This module targets the OpenSubtitles REST API v1 instead.

Opt-in: an API key (config `[enrich] opensubtitles_enabled` + the
MLO_OPENSUBTITLES_KEY env var) enables it; the offline core never needs it.
Only SEARCH is implemented — downloading and writing the actual .srt is a
filesystem mutation and therefore a gated kernel step, deferred (same
posture as tmdb.py's poster/nfo render: this returns metadata, never bytes
to write).
"""
from __future__ import annotations

import os
import urllib.parse

from . import NETWORK_ERRORS, get_json

API = "https://api.opensubtitles.com/api/v1"
USER_AGENT = "mlo/0.2 (https://github.com/msampath/library-organizer)"


def _key(key: str | None) -> str | None:
    return key or os.environ.get("MLO_OPENSUBTITLES_KEY")


def search_subtitles(title: str, year: int | None = None, lang: str = "en", *,
                     key: str | None = None, transport=None,
                     timeout: int = 20) -> list[dict]:
    """Subtitle listings matching `title` (+optional year, language), or [].
    A missing key, empty title, or any network failure degrades to [] — the
    caller stays offline-safe. Returns metadata only (release name, language,
    file id/name); downloading the subtitle text itself is a future gated
    kernel step, not implemented here."""
    k = _key(key)
    if not k or not title:
        return []
    transport = transport or get_json
    params = {"query": title, "languages": lang}
    if year:
        params["year"] = year
    url = f"{API}/subtitles?{urllib.parse.urlencode(params)}"
    try:
        data = transport(url, {"Api-Key": k, "User-Agent": USER_AGENT,
                               "Accept": "application/json"}, timeout)
    except NETWORK_ERRORS:
        return []
    if not isinstance(data, dict):
        return []          # malformed body degrades like a network failure
    out: list[dict] = []
    for item in data.get("data") or []:
        attrs = item.get("attributes") or {}
        files = attrs.get("files") or []
        out.append({
            "id": item.get("id"),
            "language": attrs.get("language"),
            "release": attrs.get("release"),
            "file_id": files[0].get("file_id") if files else None,
            "file_name": files[0].get("file_name") if files else None,
        })
    return out
