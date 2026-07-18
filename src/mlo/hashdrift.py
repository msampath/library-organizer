"""Hash-drift mitigation (§5.2 / §5.4).

Embedding metadata (an ID3 tag, an EXIF date) rewrites the file's bytes, so its
fingerprint changes. If the library index is not updated in the same breath, a
later dedup either treats the enriched file as changed content (a false
non-match) or, worse, as a twin it no longer is. recompute() re-fingerprints an
indexed file and updates its index row transactionally. It is INDEX-only — it
never touches the file — so any enrichment write-back (a future gated kernel op)
calls this immediately after it writes, and dedup stays honest.
"""
from __future__ import annotations

import os

from . import fingerprint
from .config import Config
from .store import Store


def recompute(store: Store, cfg: Config, relpath: str) -> tuple[str, str] | None:
    """Re-fingerprint a library file and update its index row. Returns
    (old_quick_hash, new_quick_hash), or None if the file isn't indexed or is
    unreadable. Index-only; never mutates the file."""
    row = store.index_get(relpath)
    if row is None:
        return None
    abspath = os.path.join(cfg.library_root, relpath)
    try:
        size, qh = fingerprint.quick(abspath)
        mtime_ns = os.stat(abspath).st_mtime_ns
    except OSError:
        return None
    old = row["quick_hash"]
    store.index_upsert(relpath, size, qh, mtime_ns, "hashdrift")
    store.index_commit()
    return old, qh
