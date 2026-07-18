"""TMDb connector — movie/TV identity + artwork. Fetch / parse / render only.

Opt-in: a key (config or `MLO_TMDB_KEY`) enables it; the offline core never
needs it. This module NEVER writes a file — it returns metadata, a poster URL,
and a rendered .nfo STRING; writing those into the library is a gated kernel
step (deferred). Missing key / offline / no result -> clean None.
"""
from __future__ import annotations

import os
import urllib.parse
import xml.etree.ElementTree as ET

from . import NETWORK_ERRORS, get_json

API = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p/w500"


def _key(key: str | None) -> str | None:
    return key or os.environ.get("MLO_TMDB_KEY")


def search_movie(title: str, year: int | None = None, *, key: str | None = None,
                 transport=None, timeout: int = 20) -> dict | None:
    """Best TMDb match for a movie title (+optional year), or None. A missing
    key or any network failure returns None — the caller degrades to offline."""
    k = _key(key)
    if not k or not title:
        return None
    transport = transport or get_json
    params = {"query": title}
    if year:
        params["year"] = year
    url = f"{API}/search/movie?{urllib.parse.urlencode(params)}"
    try:
        data = transport(url, {"Authorization": f"Bearer {k}",
                               "accept": "application/json"}, timeout)
    except NETWORK_ERRORS:
        return None
    results = data.get("results") or []
    if not results:
        return None
    top = results[0]
    release = top.get("release_date") or ""
    return {"tmdb_id": top.get("id"),
            "title": top.get("title"),
            "year": int(release[:4]) if release[:4].isdigit() else None,
            "overview": top.get("overview"),
            "poster_path": top.get("poster_path")}


def poster_url(meta: dict, base: str = IMG_BASE) -> str | None:
    p = meta.get("poster_path")
    return (base + p) if p else None


def render_nfo(meta: dict) -> str:
    """A Jellyfin/Kodi movie .nfo document as a STRING (pure). The caller writes
    it through the kernel; this never touches disk."""
    root = ET.Element("movie")
    for tag, val in (("title", meta.get("title")),
                     ("year", meta.get("year")),
                     ("plot", meta.get("overview")),
                     ("tmdbid", meta.get("tmdb_id"))):
        if val is not None and val != "":
            ET.SubElement(root, tag).text = str(val)
    return ET.tostring(root, encoding="unicode")
