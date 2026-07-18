"""Hint assembly — advisory metadata gathered at the EDGE and handed to the
pure router as taxonomy.Hints (the engine reads no metadata inside route()).

Shared by the CLI (`plan --hints/--exif/--sniff`) and the pilot orchestrator so
there is exactly one mechanism (harness: same seam, no parallel implementations).
Every function is read-only I/O: EXIF years, magic-byte sniffs, embedded doc
properties, or a user-authored hints JSON. Nothing here mutates anything.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys

from .config import Config, ConfigError
from .store import Store
from .taxonomy import Hints, bucket_for


def load_hints(path: str | None) -> dict:
    """Hints JSON ({relpath: {media_kind, language, year}}) -> Hints map.
    Advisory input assembled by `mlo agent classify` or by hand; unknown keys
    per entry are rejected so a typo can't silently drop a hint."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"cannot read hints file {path}: {e}")
    out = {}
    for rel, h in raw.items():
        if not isinstance(h, dict):
            raise ConfigError(f"hints[{rel!r}] must be an object")
        unknown = set(h) - {"media_kind", "language", "year", "content_kind",
                           "book_author", "book_title", "book_series",
                           "book_index"}
        if unknown:
            raise ConfigError(f"hints[{rel!r}]: unknown keys {sorted(unknown)}")
        year = h.get("year")
        if year is not None and (isinstance(year, bool)
                                 or not isinstance(year, int)
                                 or not 1900 <= year <= 2035):
            raise ConfigError(
                f"hints[{rel!r}]: year must be null or an integer 1900-2035, "
                f"got {year!r}")
        content_kind = h.get("content_kind")
        if content_kind is not None and content_kind not in (
                "video", "audio", "image"):
            raise ConfigError(
                f"hints[{rel!r}]: content_kind must be null or one of "
                f"video/audio/image, got {content_kind!r}")
        for k in ("book_author", "book_title", "book_series"):
            v = h.get(k)
            if v is not None and not isinstance(v, str):
                raise ConfigError(f"hints[{rel!r}]: {k} must be a string or null")
        book_index = h.get("book_index")
        if book_index is not None and (isinstance(book_index, bool)
                                       or not isinstance(book_index, int)
                                       or book_index < 0):
            raise ConfigError(
                f"hints[{rel!r}]: book_index must be null or an integer >= 0, "
                f"got {book_index!r}")
        out[rel.replace("/", os.sep)] = Hints(
            media_kind=h.get("media_kind"),
            language=h.get("language"),
            year=year,
            content_kind=content_kind,
            book_author=h.get("book_author"),
            book_title=h.get("book_title"),
            book_series=h.get("book_series"),
            book_index=book_index)
    return out


def photo_exts(cfg: Config) -> set[str]:
    exts: set[str] = set()
    for label in ("Photos", "Images"):
        exts.update(cfg.taxonomy.get(label, ()))
    return exts


def book_exts(cfg: Config) -> set[str]:
    return set(cfg.taxonomy.get("Ebooks", ()))


def augment_bookmeta_library(cfg: Config, store: Store, under: list[str],
                             hints: dict, verbose: bool = False) -> dict:
    """Book identity (embedded metadata or filename parse — bookmeta.identity)
    for in-scope Ebooks-bucket files that don't already carry a book_author
    hint. Mirrors augment_exif_library's fill-only-when-absent discipline: a
    hint already carrying book_author (from a hand-authored file or an Opus
    subagent judgment) is never overwritten."""
    from . import bookmeta
    exts = book_exts(cfg)
    if not exts:
        return hints
    prefixes = [p.replace("/", os.sep).rstrip(os.sep) + os.sep for p in under]
    n = 0
    for row in store.index_iter():
        rel = row["relpath"]
        if prefixes and not any(os.path.normcase(rel).startswith(os.path.normcase(p))
                                for p in prefixes):
            continue
        if os.path.splitext(rel)[1].lower() not in exts:
            continue
        h = hints.get(rel)
        if h is not None and h.book_author is not None:
            continue
        if verbose:
            print(f"  bookmeta: {rel}", file=sys.stderr)
        ident = bookmeta.identity(os.path.join(cfg.library_root, rel),
                                  os.path.basename(rel))
        if ident.get("author") is None and ident.get("title") is None:
            continue
        # dataclasses.replace on the existing hint: every field the caller
        # already set survives, and each book field fills ONLY when absent —
        # a hand-authored title must never be clobbered by an extracted one
        # (the fill-only discipline, per field; super-review B-051).
        base = h or Hints()
        hints[rel] = dataclasses.replace(
            base,
            book_author=base.book_author if base.book_author is not None
                        else ident.get("author"),
            book_title=base.book_title if base.book_title is not None
                       else ident.get("title"),
            book_series=base.book_series if base.book_series is not None
                        else ident.get("series"),
            book_index=base.book_index if base.book_index is not None
                       else ident.get("series_index"))
        n += 1
    if n:
        print(f"bookmeta: {n} book identit{'y' if n == 1 else 'ies'} read")
    return hints


def augment_exif_library(cfg: Config, store: Store, under: list[str],
                         hints: dict, verbose: bool = False) -> dict:
    """EXIF years for in-scope library photos that don't already have one."""
    from . import exif
    exts = photo_exts(cfg)
    prefixes = [p.replace("/", os.sep).rstrip(os.sep) + os.sep for p in under]
    n = 0
    for row in store.index_iter():
        rel = row["relpath"]
        if prefixes and not any(os.path.normcase(rel).startswith(os.path.normcase(p))
                                for p in prefixes):
            continue
        if os.path.splitext(rel)[1].lower() not in exts:
            continue
        h = hints.get(rel)
        if h is not None and h.year is not None:
            continue
        if verbose:
            print(f"  exif: {rel}", file=sys.stderr)
        year = exif.year_of(os.path.join(cfg.library_root, rel))
        if year is not None:
            # replace(), not a hand-listed rebuild: content_kind/book_* on an
            # existing hint must survive an EXIF year (super-review B-050).
            hints[rel] = dataclasses.replace(h or Hints(), year=year)
            n += 1
    if n:
        print(f"exif: {n} photo year(s) read")
    return hints


def augment_exif_source(cfg: Config, store: Store, source_name: str,
                        hints: dict, verbose: bool = False) -> dict:
    """EXIF years for a source's UNIQUE photos (organize path)."""
    from . import exif
    exts = photo_exts(cfg)
    root = cfg.source(source_name).root
    n = 0
    for row in store.source_iter(source_name, "UNIQUE"):
        rel = row["relpath"]
        if os.path.splitext(rel)[1].lower() not in exts:
            continue
        h = hints.get(rel)
        if h is not None and h.year is not None:
            continue
        if verbose:
            print(f"  exif: {rel}", file=sys.stderr)
        year = exif.year_of(os.path.join(root, rel))
        if year is not None:
            hints[rel] = dataclasses.replace(h or Hints(), year=year)
            n += 1
    if n:
        print(f"exif: {n} photo year(s) read")
    return hints


def augment_sniff_library(cfg: Config, store: Store, under: list[str],
                          hints: dict, min_mb: float = 0.0,
                          verbose: bool = False) -> dict:
    """Content-sniff in-scope library files that have NO taxonomy bucket, so a
    false-carve routes by magic bytes. Only unbucketed files are sniffed — a
    configured extension already decides, and content never overrides it.
    `min_mb` skips files below that size (media carves are large; tiny files
    with weak signatures are the false-positive risk)."""
    from . import sniff
    prefixes = [p.replace("/", os.sep).rstrip(os.sep) + os.sep for p in under]
    min_bytes = int(min_mb * 1024 * 1024)
    n = 0
    for row in store.index_iter():
        rel = row["relpath"]
        if prefixes and not any(os.path.normcase(rel).startswith(os.path.normcase(p))
                                for p in prefixes):
            continue
        if row["size"] < min_bytes:
            continue
        if os.path.splitext(rel)[1].lower() in sniff.NEVER_MEDIA_EXTS:
            continue                         # never a carve (log/db/executable)
        if bucket_for(cfg, rel) is not None:
            continue                         # extension decides; don't override it
        h = hints.get(rel)
        if h is not None and h.content_kind is not None:
            continue
        if verbose:
            print(f"  sniff: {rel}", file=sys.stderr)
        kind = sniff.kind_of(os.path.join(cfg.library_root, rel))
        if kind is not None:
            hints[rel] = dataclasses.replace(h or Hints(), content_kind=kind)
            n += 1
    if n:
        print(f"sniff: {n} false-carve(s) identified by content")
    return hints


def doc_props_map(base_root: str, rows) -> dict[str, dict]:
    """relpath -> embedded document properties, for review-set enrichment
    (CANONICAL: critics judge with all signals). Only document extensions are
    opened (docmeta.DOC_EXTS) — media/blob files are skipped, and a file with
    no properties simply doesn't appear."""
    from . import docmeta
    out: dict[str, dict] = {}
    for r in rows:
        rel = r["relpath"]
        if os.path.splitext(rel)[1].lower() not in docmeta.DOC_EXTS:
            continue
        p = docmeta.props(os.path.join(base_root, rel))
        if p:
            out[rel] = p
    return out
