"""Builders for plan/apply/verify tests: a seeded config + store on fake drives."""
from __future__ import annotations

import os
from pathlib import Path

from conftest import make_file
from helpers import make_cfg as _base_cfg
from mlo import fingerprint
from mlo.config import Config, Source


def make_cfg(world, max_unmatched_pct: float = 50.0) -> Config:
    """The pipeline-test config, built from the one base in helpers.make_cfg so
    the two never silently diverge under the same name. Overrides: a nested
    source root (so staging isn't a sibling), a `.vob` bucket, and — because
    these fixtures physically run on a real drive with an injected fake
    drive_of — no protected DRIVES (a real-letter block would false-protect
    every tmp path; drive-block behavior is covered in test_safeops)."""
    return _base_cfg(
        world,
        sources=(Source("eSrc", str(world["E"] / "src"), True),),
        protected_drives=(),
        junk_names=("thumbs.db",),
        max_unmatched_pct=max_unmatched_pct,
        taxonomy={"Video": (".mp4", ".mkv", ".vob"), "Audio": (".mp3",),
                  "Documents": (".pdf", ".txt")},
    )


def seed_source(world, cfg, files: dict[str, bytes]) -> dict[str, tuple[int, str]]:
    """Create files under the source root; returns relpath -> (size, quick_hash)."""
    root = Path(cfg.sources[0].root)
    out = {}
    for rel, content in files.items():
        p = make_file(root / rel, content)
        out[rel.replace("/", os.sep)] = fingerprint.quick(str(p))
    return out


def seed_store(world, cfg, pre: dict[str, tuple[int, str]],
               verdicts: dict[str, tuple[str, str]],
               library_rows: dict[str, tuple[int, str]] | None = None) -> None:
    """Populate source_files + verdicts + fresh artifacts (and optionally the
    library index) as a completed scan+verdict pass would have."""
    st = world["store"]
    src = cfg.sources[0].name
    for rel, (size, qh) in pre.items():
        st.source_upsert(src, rel, size, qh, 0, "scan-seed")
    st.source_commit()
    for rel, (verdict, rule) in verdicts.items():
        st.source_set_verdict(src, rel.replace("/", os.sep), verdict, rule)
    st.source_commit()
    for rel, (size, qh) in (library_rows or {}).items():
        st.index_upsert(rel.replace("/", os.sep), size, qh, 0, "scan-seed")
    st.index_commit()
    run = st.start_run("seed", [], cfg.config_hash, "test")
    st.artifact_register(f"scan:{src}", "scan",
                         {"root": cfg.sources[0].root}, cfg.config_hash, run)
    st.artifact_register("index:library", "index",
                         {"root": cfg.library_root}, cfg.config_hash, run)
    st.artifact_register(f"verdicts:{src}", "verdicts",
                         {"root": cfg.sources[0].root}, cfg.config_hash, run)
