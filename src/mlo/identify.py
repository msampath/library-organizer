"""mlo identify — the productized identification loop (P21/B6).

The first-class version of the out-of-engine judgment loop the owner ran by
hand for the P17 Ebooks migration and others: a review-set sliced into
batches, judged by a critic chain, merged into ONE hints file, schema-gated,
then fed back through `mlo pilot --hints`/`mlo plan reorganize --hints`.
Before this module the only way to run that loop was to hand-author or
hand-merge JSON — this closes that friction (G219/G511).

Pure orchestration: no new filesystem power beyond a single JSON write (via
report.write_json, the same helper `mlo agent critics` already uses). It
consumes the existing seam (review-set items already on disk),
agent/critics (the panel), and hints (the schema gate) — the same pieces
pilot.analyze's A6/A7 already compose internally, but runnable STANDALONE
over a review-set.jsonl artifact from a prior run, in batches, with an
optional prior-hints seed for incremental resumption.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .agent.critics import run_panel
from .agent.llm import ChainClient, chain_config
from .config import Config, ConfigError
from .hints import load_hints
from .taxonomy import Hints


@dataclass
class IdentifyResult:
    hinted: int
    unsure: list[str] = field(default_factory=list)
    dissent: list[dict] = field(default_factory=list)
    batches: int = 0
    items: int = 0


def _hints_to_raw(h: Hints) -> dict:
    """A Hints dataclass instance back to the plain-dict hints-JSON shape
    (the inverse of hints.load_hints's per-key parse), so a prior hints file
    can seed the merge alongside fresh run_panel dict output."""
    return {k: v for k, v in {
        "media_kind": h.media_kind, "language": h.language, "year": h.year,
        "content_kind": h.content_kind, "book_author": h.book_author,
        "book_title": h.book_title, "book_series": h.book_series,
        "book_index": h.book_index}.items() if v is not None}


def read_review_set(path: str) -> list[dict]:
    """Parse a review-set.jsonl artifact (report.write_review_set's format):
    one JSON object per line, an optional {"schema": ...} header on line 1."""
    items: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if "schema" in obj and "relpath" not in obj:
                    continue                 # header line, not an item
                items.append(obj)
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"cannot read review-set {path}: {e}")
    return items


def identify(cfg: Config, review_set_path: str, *,
            chain: tuple[str, ...] | None = None,
            batch_size: int = 500,
            cross_check: bool = False,
            prior_hints_path: str | None = None,
            progress=None) -> tuple[dict, IdentifyResult]:
    """Slice the review-set at `review_set_path` into `batch_size`-item
    batches, run the configured critic chain over each, and merge the
    resulting hints into ONE dict (plain hints-JSON shape). Returns
    (merged_hints, result_summary) — the caller writes merged_hints to disk
    (report.write_json) and validates it through hints.load_hints (the
    schema gate) before trusting it for a re-plan; see cli.py's `identify`
    command for the reference sequence."""
    items = read_review_set(review_set_path)
    batch_size = max(1, batch_size)      # 0/negative would loop empty batches
    ccfg = chain_config(cfg, chain)
    client = ChainClient(ccfg)

    merged: dict[str, dict] = {}
    if prior_hints_path:
        for rel, h in load_hints(prior_hints_path).items():
            raw = _hints_to_raw(h)
            if raw:
                merged[rel] = raw
        # Incremental resumption means NOT re-paying the critic chain for
        # items the prior hints already answered (super-review M12) — the
        # docstring's promise, now actually kept.
        items = [it for it in items if it.get("relpath") not in merged]

    all_unsure: list[str] = []
    all_dissent: list[dict] = []
    n_batches = 0
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        n_batches += 1
        if progress:
            progress("identify-batch", {"n": n_batches, "size": len(batch)})
        # evidence must be extracted into its own dict (run_panel's contract,
        # matching pilot.py's A6 call site) — an item's item['evidence'] alone
        # is not enough; run_panel never reads it off the item directly.
        out = run_panel(client, cfg, batch,
                        evidence={it["relpath"]: it.get("evidence", {})
                                 for it in batch},
                        cross_check=cross_check)
        for rel, h in out["hints"].items():
            merged[rel] = h
        all_unsure.extend(out["unsure"])
        all_dissent.extend(out["dissent"])

    return merged, IdentifyResult(
        hinted=len(merged), unsure=all_unsure, dissent=all_dissent,
        batches=n_batches, items=len(items))
