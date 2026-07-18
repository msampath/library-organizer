"""Audio-tag connector — mutagen ID3 read. Read only (no write-back here).

mutagen is an optional dependency (mlo[enrich]); without it read_tags returns
None so the offline core is unaffected. Write-back (embedding a corrected tag)
is a filesystem mutation and therefore a gated kernel step, deferred — and when
it lands it MUST call mlo.hashdrift.recompute, because embedding a tag changes
the file's bytes and its fingerprint.
"""
from __future__ import annotations

from .. import winpath

_FIELDS = ("artist", "album", "title", "genre", "date")


def read_tags(path: str) -> dict | None:
    """Common audio tags as a dict, or None (mutagen absent / unreadable / no
    tags). Total on the core path — never raises."""
    try:
        import mutagen                                   # optional dependency
    except ImportError:
        return None
    try:
        m = mutagen.File(winpath.to_long(path), easy=True)
    except Exception:                                    # mutagen raises many types
        return None
    if m is None:
        return None
    out: dict[str, str | None] = {}
    for field in _FIELDS:
        val = m.get(field)
        out[field] = val[0] if val else None
    return out
