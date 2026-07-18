"""Source-drive sweep: the productized consolidation orchestration.

`mlo sweep` is the reviewed, journaled, resumable form of "consolidate a source
drive into the library and stage the already-present originals out" — the
workflow that must NEVER live in an ad-hoc operator script (the L0 lesson:
safety is constructed, not practiced). It composes the existing gated primitives
— scan -> verdict -> dedup with `--confirm-mb` -> apply — and adds no new
filesystem power (the kernel is still the only door).

Two safety postures are baked in, not left to operator discipline:
  - UNIQUE files (the only copy) are HELD, never auto-copied into the curated
    library. Preserving them is a human decision (`mlo plan organize`), because
    what enters the library is a judgment call — a sweep must not launder an
    unvetted only-copy into it.
  - every ORGANIZED original re-confirms against its library twin at
    `confirm_bytes` head+tail before it is staged out (the 1 MiB bar), so a
    same-size / same-ends / different-middle file is never swept off its only
    unique content.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import apply as applymod
from . import scan, verdict
from .config import Config
from .plan import build_dedup
from .store import Store


@dataclass
class SweepEntry:
    source: str
    verdicts: dict[str, int] = field(default_factory=dict)
    would_stage: int = 0
    staged: int = 0
    confirm_failed: int = 0
    held: bool = False              # UNIQUE present -> preserve first, not swept
    status: str = ""


def sweep(store: Store, cfg: Config, run_id: str, sources=None,
          confirm_bytes: int = 0, execute: bool = False,
          waive_organize: bool = False) -> list[SweepEntry]:
    """Sweep the named sources (or every enabled source). Read-only rehearsal
    unless execute=True. Returns one SweepEntry per source.

    By default a source with UNIQUE (only-copy) files is HELD untouched. With
    waive_organize=True the proven-in-library duplicates (ORGANIZED + JUNK) are
    staged out anyway and the UNIQUE files are LEFT IN PLACE (never staged — they
    are not dedup rows) and reported — for when the uniques are a human-review
    pile (e.g. recovery carves) you want kept where they are while the real
    duplicates go."""
    names = list(sources) if sources else [s.name for s in cfg.sources if s.enabled]
    # verdicts need a fresh library index; refresh it read-only if the config
    # churned (adding a source restamps config_hash). scan_library has a stat
    # fast-path, so an unchanged library re-registers without re-hashing.
    if not store.artifact_fresh("index:library", cfg.config_hash):
        scan.scan_library(store, cfg, run_id)
    if execute:
        store.snapshot()

    entries: list[SweepEntry] = []
    for name in names:
        scan.scan_source(store, cfg, name, run_id)
        counts = verdict.assign(store, cfg, name, run_id)
        e = SweepEntry(name, counts)
        uniq = counts.get("UNIQUE", 0)
        if uniq and not waive_organize:
            e.held = True
            e.status = (f"HELD: {uniq} unique (only copy) — preserve with "
                        f"`mlo plan organize {name}` + apply, then re-sweep")
            entries.append(e)
            continue
        res = build_dedup(store, cfg, name, waive_organize=waive_organize,
                          confirm_bytes=confirm_bytes)
        e.would_stage = res.n_rows
        e.confirm_failed = res.confirm_failed
        left = f", {uniq} unique left in place" if uniq else ""
        if execute:
            ares = applymod.apply_plan(store, cfg, res.path, run_id, execute=True)
            e.staged = ares.counts.get("done", 0)
            base = "swept" if ares.exit_code == 0 else "swept (residuals)"
            e.status = base + left
        else:
            e.status = "would stage" + left
        entries.append(e)
    return entries
