"""Verdict assignment: ORGANIZED / JUNK / UNIQUE / REVIEW per source file.

Consumes only fresh artifacts (defect L7): both the source's scan and the
library index must be fresh for the current config, or StaleArtifactError
names the exact remedy command. Verdicts land in the store with rule
provenance and are stamped as a fresh 'verdicts:<source>' artifact.

Order of precedence per file:
  junk rule -> JUNK · fingerprint in library index -> ORGANIZED ·
  taxonomy bucket -> UNIQUE · nothing matched -> REVIEW (never a silent bin).
"""
from __future__ import annotations

from . import taxonomy
from .config import Config
from .store import Store


class StaleArtifactError(Exception):
    """A consumer refused a stale/missing artifact. CLI maps this to exit 4."""


def _require_fresh(store: Store, artifact_id: str, config_hash: str,
                   remedy: str) -> None:
    if not store.artifact_fresh(artifact_id, config_hash):
        a = store.artifact_get(artifact_id)
        state = a.status if a else "missing"
        raise StaleArtifactError(
            f"artifact '{artifact_id}' is {state} for this config — "
            f"refusing stale input. Remedy: {remedy}")


def assign(store: Store, cfg: Config, source_name: str, run_id: str
           ) -> dict[str, int]:
    source = cfg.source(source_name)
    _require_fresh(store, f"scan:{source_name}", cfg.config_hash,
                   f"mlo scan {source_name}")
    _require_fresh(store, "index:library", cfg.config_hash, "mlo scan library")

    counts: dict[str, int] = {"ORGANIZED": 0, "JUNK": 0, "UNIQUE": 0, "REVIEW": 0}
    # Materialize before updating: sqlite may re-yield rows UPDATEd while a
    # live cursor iterates the same table (rows can move in the b-tree).
    for row in list(store.source_iter(source_name)):
        rel = row["relpath"]
        junk_rule = taxonomy.classify_junk(cfg, rel, row["size"])
        if junk_rule:
            verdict, rule = "JUNK", junk_rule
        elif store.index_lookup(row["size"], row["quick_hash"]):
            verdict, rule = "ORGANIZED", "fp-match"
        else:
            bucket = taxonomy.bucket_for(cfg, rel)
            if bucket:
                verdict, rule = "UNIQUE", f"bucket:{bucket[0]}"
            else:
                verdict, rule = "REVIEW", "no-rule-matched"
        store.source_set_verdict(source_name, rel, verdict, rule)
        counts[verdict] += 1

    store.source_commit()
    store.artifact_register(f"verdicts:{source_name}", "verdicts",
                            {"root": source.root}, cfg.config_hash, run_id,
                            "fresh")
    return counts
