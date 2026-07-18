"""Walkers that populate the store. Read/stat only — the kernel owns mutation.

scan_library: fingerprint the library into the index with a stat fast-path
(size+mtime unchanged => no re-hash). Index rows whose files have vanished from
disk are deliberately NOT removed here: the index is maintained transactionally
by engine ops (defect L7), and *external* deletions are a finding that
verify.py owns reporting — a scanner silently shrinking the index would hide
them.

scan_source: full re-fingerprint of a source root every time (verdicts must
reflect current content; source_upsert resets any prior verdict), then drops
rows for files no longer present, then stamps the scan artifact fresh.

Both prune protected paths and staging roots by CONFIG, not by any hardcoded
literal (defect L6): a staged file must never be re-scanned as source content
(that would re-stage it forever — defect L2) nor indexed as library content.

Interrupted scans leave their artifacts 'building'/'stale', never 'fresh', so a
half-populated store can never be consumed as truth (defect L7/C6). Both return
(count, skipped_paths) where skipped_paths raised OSError while hashing.
"""
from __future__ import annotations

import os
from typing import Callable

from . import fingerprint, winpath
from .config import Config, ConfigError
from .store import Store

BATCH = 500


def _prune_predicate(cfg: Config, exclude_roots: list[str]):
    subs = tuple(s for s in cfg.protected_substrings if s)
    exclude = [os.path.abspath(r) for r in exclude_roots]

    def skip_dir(plain_dir_path: str, name: str) -> bool:
        low = name.lower()
        if any(s in low for s in subs):
            return True
        full = os.path.abspath(os.path.join(plain_dir_path, name))
        return any(winpath.is_under(full, r) for r in exclude)

    return skip_dir


def _walk_files(root: str, skip_dir):
    """Yield (plain_abs_path, stat_result|None) for every file under root,
    pruning directories the skip_dir predicate rejects."""
    long_root = winpath.to_long(root)
    for dirpath, dirnames, filenames in os.walk(long_root):
        plain_dir = winpath.from_long(dirpath)
        dirnames[:] = [d for d in dirnames if not skip_dir(plain_dir, d)]
        for name in filenames:
            long_full = os.path.join(dirpath, name)
            try:
                st = os.stat(long_full)
            except OSError:
                yield os.path.join(plain_dir, name), None
                continue
            yield os.path.join(plain_dir, name), st


def scan_library(store: Store, cfg: Config, run_id: str,
                 progress: Callable[[int], None] | None = None,
                 rehash_under: list[str] | None = None
                 ) -> tuple[int, list[str]]:
    """rehash_under: prefixes whose files bypass the stat fast-path and are
    re-hashed unconditionally — the refresh for SILENT content change (bit-rot,
    a torn earlier read), which same-size+same-mtime otherwise hides forever
    (found live: a 2006 .ppt whose bytes differed from its stored hash with
    both size and mtime unchanged, wedging every plan into skipped_drift)."""
    root = os.path.abspath(cfg.library_root)
    skip_dir = _prune_predicate(cfg, list(cfg.staging.values()))
    force = [p.replace("/", os.sep).rstrip(os.sep) + os.sep
             for p in (rehash_under or [])]
    known = {row["relpath"]: (row["size"], row["mtime_ns"])
             for row in store.index_iter()}
    store.artifact_register("index:library", "index", {"root": cfg.library_root},
                            cfg.config_hash, run_id, "building")
    indexed = 0
    pending = 0
    skipped: list[str] = []

    for plain_full, st in _walk_files(root, skip_dir):
        rel = os.path.relpath(plain_full, root)
        if st is None:
            skipped.append(plain_full)
            continue
        prior = known.get(rel)
        if prior is not None and prior == (st.st_size, st.st_mtime_ns) \
                and not any(os.path.normcase(rel).startswith(os.path.normcase(f))
                            for f in force):
            continue  # stat fast-path: unchanged, no re-hash
        try:
            size, qh = fingerprint.quick(plain_full)
        except OSError:
            skipped.append(plain_full)
            continue
        store.index_upsert(rel, size, qh, st.st_mtime_ns, run_id)
        indexed += 1
        pending += 1
        if pending >= BATCH:
            store.index_commit()
            pending = 0
            if progress:
                progress(indexed)

    store.index_commit()
    store.artifact_register("index:library", "index", {"root": cfg.library_root},
                            cfg.config_hash, run_id, "fresh")
    if progress:
        progress(indexed)
    return indexed, skipped


def scan_source(store: Store, cfg: Config, source_name: str, run_id: str,
                progress: Callable[[int], None] | None = None
                ) -> tuple[int, list[str]]:
    source = cfg.source(source_name)
    if not source.enabled:
        raise ConfigError(
            f"source '{source_name}' is disabled (enabled = false); "
            f"enable it before scanning")
    root = os.path.abspath(source.root)
    # Never re-scan staged content or the library as if it were source input.
    skip_dir = _prune_predicate(
        cfg, list(cfg.staging.values()) + [cfg.library_root])

    scope = {"root": source.root, "name": source_name}
    store.artifact_register(f"scan:{source_name}", "scan", scope,
                            cfg.config_hash, run_id, "building")
    # A rescan invalidates prior verdicts; mark them stale up front so an
    # interrupted rescan can never leave stale verdicts looking fresh (C6).
    if store.artifact_get(f"verdicts:{source_name}") is not None:
        store.artifact_set_status(f"verdicts:{source_name}", "stale")

    scanned = 0
    pending = 0
    skipped: list[str] = []

    for plain_full, st in _walk_files(root, skip_dir):
        if st is None:
            skipped.append(plain_full)
            continue
        rel = os.path.relpath(plain_full, root)
        try:
            size, qh = fingerprint.quick(plain_full)
        except OSError:
            skipped.append(plain_full)
            continue
        store.source_upsert(source_name, rel, size, qh, st.st_mtime_ns, run_id)
        scanned += 1
        pending += 1
        if pending >= BATCH:
            store.source_commit()
            pending = 0
            if progress:
                progress(scanned)

    store.source_commit()
    store.source_delete_not_in_scan(source_name, run_id)
    store.artifact_register(f"scan:{source_name}", "scan", scope,
                            cfg.config_hash, run_id, "fresh")
    if progress:
        progress(scanned)
    return scanned, skipped
