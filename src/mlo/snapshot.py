"""Library-state snapshot (E1) — the machine-readable "current shit state".

For each folder: file/byte counts, extension and bucket histograms, its current
placement, and — when the folder's content clearly belongs elsewhere (media
sitting in a generic bin like Other/Unsorted) — a suspected home and a
confidence. This is the inventory the self-improving loop reads to know what to
fix, and the visibility the requirement asks for ("the current shit state has
to be clearly available"). Pure: reads the index, writes nothing (report writes
the JSON).
"""
from __future__ import annotations

import os
from collections import Counter

from . import taxonomy
from .config import Config
from .store import Store

_AREA_ROOT = {"Video": "movies_root", "Videos": "movies_root",
              "Audio": "music_root",
              "Photos": "photos_root", "Images": "photos_root"}


def _area_top(cfg: Config, bucket: str | None) -> str | None:
    if bucket not in _AREA_ROOT:
        return None
    return getattr(cfg.layout, _AREA_ROOT[bucket]).replace(
        "\\", "/").split("/")[0]


def build_snapshot(store: Store, cfg: Config, under: str | None = None,
                   depth: int = 2) -> dict:
    """A per-folder problem inventory over the library index."""
    pref = None
    if under:
        pref = os.path.normcase(under.replace("/", os.sep).rstrip(os.sep) + os.sep)

    groups: dict[str, dict] = {}
    for row in store.index_iter():
        rel = row["relpath"]
        if pref and not os.path.normcase(rel).startswith(pref):
            continue
        parts = rel.replace("/", os.sep).split(os.sep)
        folder = (os.sep.join(parts[:depth]) if len(parts) > depth
                  else (os.sep.join(parts[:-1]) or "(root)"))
        g = groups.setdefault(folder, {"files": 0, "bytes": 0,
                                       "ext": Counter(), "bucket": Counter()})
        g["files"] += 1
        g["bytes"] += row["size"]
        g["ext"][os.path.splitext(rel)[1].lower() or "(none)"] += 1
        bucket = taxonomy.bucket_for(cfg, rel)
        g["bucket"][bucket[0] if bucket else "(unmatched)"] += 1

    folders = []
    for folder, g in groups.items():
        dominant, dom_n = (g["bucket"].most_common(1)[0]
                           if g["bucket"] else ("(unmatched)", 0))
        frac = dom_n / g["files"] if g["files"] else 0.0
        area = _area_top(cfg, dominant)
        top_seg = folder.split(os.sep)[0]
        misplaced = bool(area) and top_seg.casefold() != area.casefold()
        folders.append({
            "folder": folder,
            "files": g["files"],
            "bytes": g["bytes"],
            "by_ext": dict(g["ext"].most_common(8)),
            "by_bucket": dict(g["bucket"]),
            "dominant_bucket": dominant,
            "current_placement": top_seg,
            "suspected_home": area if misplaced else None,
            "confidence": round(frac, 2),
            "needs_human": frac < 0.6,
            "problem": misplaced,
        })
    folders.sort(key=lambda f: -f["bytes"])
    problems = [f for f in folders if f["problem"]]
    return {"root": cfg.library_root, "scoped_under": under,
            "folders": folders, "problem_count": len(problems),
            "total_files": sum(f["files"] for f in folders)}
