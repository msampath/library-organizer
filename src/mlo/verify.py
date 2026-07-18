"""Verification: the library vs its index, and staging vs the rules.

Read-only by construction (no kernel is ever built here — the architecture
test would reject one anyway). Findings, not mutations:

  - external edits (defect L14): files on disk missing from the index, index
    rows missing on disk, size/mtime drift (--quick);
  - .mlopart residue: a failed copy's inert leftover (kernel L15 semantics);
  - protected content inside staging roots (defect L12) — BLOCKING: disposal
    of staging must never destroy protected data;
  - staging content the journal cannot explain (L14) — someone moved files
    into staging outside the engine.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import fingerprint, winpath
from .config import Config
from .store import Store


@dataclass
class Findings:
    unindexed: list[str] = field(default_factory=list)      # on disk, not in index
    missing: list[str] = field(default_factory=list)        # in index, not on disk
    drifted: list[str] = field(default_factory=list)        # size/mtime mismatch
    mlopart: list[str] = field(default_factory=list)
    protected_in_staging: list[str] = field(default_factory=list)   # BLOCKING
    unjournaled_staging: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return bool(self.protected_in_staging)

    def counts(self) -> dict:
        return {
            "unindexed": len(self.unindexed),
            "missing_on_disk": len(self.missing),
            "drifted": len(self.drifted),
            "mlopart_residue": len(self.mlopart),
            "protected_in_staging": len(self.protected_in_staging),
            "unjournaled_staging": len(self.unjournaled_staging),
        }


def _walk_files(root: str, protected_subs: tuple[str, ...]):
    lroot = winpath.to_long(root)
    for dirpath, dirnames, filenames in os.walk(lroot):
        dirnames[:] = [d for d in dirnames
                       if not any(s in d.lower() for s in protected_subs)]
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def verify_library(store: Store, cfg: Config, quick: bool = True) -> Findings:
    f = Findings()
    lib = cfg.library_root
    index = {row["relpath"]: row for row in store.index_iter()}
    seen: set[str] = set()

    for lpath in _walk_files(lib, cfg.protected_substrings):
        plain = winpath.from_long(lpath)
        rel = os.path.relpath(plain, winpath.from_long(winpath.to_long(lib)))
        if plain.endswith(".mlopart"):
            f.mlopart.append(rel)
            continue
        row = index.get(rel)
        if row is None:
            f.unindexed.append(rel)
            continue
        seen.add(rel)
        try:
            st = os.stat(lpath)
        except OSError:
            f.drifted.append(rel)
            continue
        if st.st_size != row["size"] or (
                row["mtime_ns"] and st.st_mtime_ns != row["mtime_ns"]):
            f.drifted.append(rel)
        elif not quick:
            # deep: re-fingerprint to catch a same-size+mtime content change a
            # stat comparison cannot see (the stale-hash direction of L13).
            try:
                _, qh = fingerprint.quick(plain)
            except OSError:
                f.drifted.append(rel)
                continue
            if qh != row["quick_hash"]:
                f.drifted.append(rel)

    # Candidate missing: index rows the walk never matched by rel string. A
    # short-name/case/normalization mismatch between the walk's rel and the
    # index key lands a real file here too (defect C49) -- confirm absence
    # against disk (the index-driven ground truth) before reporting it.
    for rel in sorted(set(index) - seen):
        if not os.path.exists(winpath.to_long(os.path.join(lib, rel))):
            f.missing.append(rel)
    return f


def verify_staging(store: Store, cfg: Config) -> Findings:
    """Scan staging roots for protected content (L12) and content the journal
    cannot explain (L14). Journal explanation = the file is some done op's dst,
    compared on the LOSSLESS path (defect L10: display strings replace lone
    surrogates, so a surrogate-named staged file must not read as unjournaled)."""
    f = Findings()
    journaled_dsts = store.staged_dsts()
    subs = tuple(s for s in cfg.protected_substrings if s)
    for drive, root in sorted(cfg.staging.items()):
        if not os.path.isdir(winpath.to_long(root)):
            continue
        for lpath in _walk_files_including_protected(root, subs):
            plain = winpath.from_long(lpath)
            if any(s in plain.lower() for s in subs):
                f.protected_in_staging.append(plain)
                continue
            if os.path.normcase(plain) not in journaled_dsts:
                f.unjournaled_staging.append(plain)
    return f


def _walk_files_including_protected(root: str, protected_subs: tuple[str, ...]):
    """Staging scans must SEE protected names to report them (unlike library
    walks, which refuse to even descend)."""
    lroot = winpath.to_long(root)
    for dirpath, dirnames, filenames in os.walk(lroot):
        for fn in filenames:
            yield os.path.join(dirpath, fn)
        for d in dirnames:
            if any(s in d.lower() for s in protected_subs):
                # surface the directory itself, then still refuse to descend
                yield os.path.join(dirpath, d)
        dirnames[:] = [d for d in dirnames
                       if not any(s in d.lower() for s in protected_subs)]
