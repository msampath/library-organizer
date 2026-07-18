"""The engine->agents seam (§3.3): build the review-set the critics judge.

The workflow owns truth and mutation; the agents only ever see this artifact —
the REVIEW residue, each item made SELF-CONTAINED so a small local model needs
no filesystem access ("individually write down fully-qualified paths … can't go
read 5 files"). Each item carries what the engine already knows — fingerprint,
provenance origin, a language guess — and an ENUMERATED candidate-home menu the
critic PICKS from (option spaces come from config/engine state, never invented,
§2.8). This module is PURE: it reads nothing but the rows/maps handed to it and
returns data; report.write_review_set does the disk write.
"""
from __future__ import annotations

import os

from . import provenance, taxonomy
from .config import Config

_AREA_ROOT = {                       # bucket label -> the root whose top segment
    "Video": "movies_root", "Videos": "movies_root",   # names the media area
    "Audio": "music_root",
    "Photos": "photos_root", "Images": "photos_root",
    "Ebooks": "ebooks_root",         # C43: Books\ home menu for review items
}


def _area_top(cfg: Config, label: str) -> str:
    root = getattr(cfg.layout, _AREA_ROOT[label])
    return root.replace("\\", "/").split("/")[0]


def candidate_homes(cfg: Config, label: str | None, language: str | None
                    ) -> list[str]:
    """The enumerated placement menu for a media bucket — type roots under the
    guessed (or default) language, the finer sub-roots for that area, and the
    Unclassified holding pen. A non-media bucket has no content menu."""
    if label not in _AREA_ROOT:
        return []
    lay = cfg.layout
    lang = language or lay.default_language
    homes: list[str] = []
    if label in ("Video", "Videos"):
        homes += [f"{lay.movies_root}/{lang}", f"{lay.tv_root}/{lang}",
                  lay.personal_root]
    elif label == "Audio":
        homes += [f"{lay.music_root}/{lang}"]
    elif label == "Ebooks":
        homes += [lay.ebooks_root, f"{lay.ebooks_root}/Unsorted"]
    else:                            # Photos / Images
        homes += [lay.photos_root]
    top = _area_top(cfg, label)
    homes += [r for r in lay.subtypes.values()
              if r.replace("\\", "/").split("/")[0].casefold() == top.casefold()]
    homes.append(f"{top}/Unclassified")
    # de-dup, preserve order
    seen: set[str] = set()
    return [h for h in homes if not (h in seen or seen.add(h))]


def build_sibling_index(relpaths, cap: int = 20) -> dict[str, list[str]]:
    """Map each folder -> the basenames of the files it contains. The critic's
    context: a title-only song ('Break The Rules.mp3') is identified not from
    its own name but from what sits BESIDE it — Tamil/Hindi film songs make it a
    film song, not a lone British rock track."""
    idx: dict[str, list[str]] = {}
    for rel in relpaths:
        folder, _, base = rel.replace(os.sep, "/").rpartition("/")
        bucket = idx.setdefault(folder, [])
        if len(bucket) < cap:
            bucket.append(base)
    return idx


def build_review_set(cfg: Config, rows, *,
                     origin_map: dict[str, str] | None = None,
                     root: str | None = None,
                     sibling_index: dict[str, list[str]] | None = None,
                     doc_props: dict[str, dict] | None = None,
                     media_tags: dict[str, dict] | None = None,
                     title_candidates: dict[str, dict] | None = None,
                     ) -> list[dict]:
    """Enrich review rows into self-contained items.

    rows: iterable of {relpath, size, quick_hash[, mtime_ns]}. Provenance origin
    comes from `origin_map` (library residue — journal-traced) or is
    `root`/relpath (a source's REVIEW pile); pass at most one. `sibling_index`
    (from build_sibling_index) adds the folder's OTHER filenames; `doc_props`
    (relpath -> docmeta.props) adds embedded document properties (creator,
    title, company, dates). `media_tags` (relpath -> id3.read_tags, P21/B3)
    adds real embedded audio tags; `title_candidates` (relpath -> a TMDb
    match) adds a real movie-identity candidate — both injected-map style,
    same as doc_props, so this module stays pure (no I/O of its own).

    CANONICAL RULE (owner, 2026-07-09): a critic judges each file with ALL the
    signals a human would read — full path, embedded metadata, siblings, dates —
    never a filename alone. This builder is the enforcement point: whatever the
    engine knows about a file must land on its item here."""
    items: list[dict] = []
    for r in rows:
        rel = r["relpath"]
        bucket = taxonomy.bucket_for(cfg, rel)
        label = bucket[0] if bucket else None
        lang_hit = taxonomy.detect_language(cfg, rel)
        language = lang_hit[0] if lang_hit else None
        if root is not None:
            origin: str | None = os.path.join(root, rel)
        elif origin_map is not None:
            origin = provenance.origin_of(cfg, origin_map, rel)
        else:
            origin = None
        item = {
            "relpath": rel,
            "ext": os.path.splitext(rel)[1].lower(),
            "size": r.get("size"),
            "quick_hash": r.get("quick_hash"),
            "bucket": label,
            "language_guess": language,
            "origin": origin,
            "origin_signal": provenance.origin_signal(origin),
            "candidate_homes": candidate_homes(cfg, label, language),
        }
        if r.get("mtime_ns"):
            import datetime
            item["mtime"] = datetime.datetime.fromtimestamp(
                r["mtime_ns"] / 1e9, datetime.timezone.utc).strftime("%Y-%m-%d")
        if sibling_index is not None:
            posix = rel.replace(os.sep, "/")
            folder, _, base = posix.rpartition("/")
            item["siblings"] = [s for s in sibling_index.get(folder, [])
                                if s != base][:12]
        if doc_props is not None and doc_props.get(rel):
            item["doc_props"] = doc_props[rel]
        if media_tags is not None and media_tags.get(rel):
            item["media_tags"] = media_tags[rel]
        if title_candidates is not None and title_candidates.get(rel):
            item["title_candidates"] = title_candidates[rel]
        items.append(item)
    return items
