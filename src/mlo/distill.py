"""Distillation writeback (§3.3, §2.8): frontier judgment spent once, reused
free.

When a critic keeps making the same generalizable call — "everything named
`UnityAds-*` is junk", "`^VID-\\d{8}-WA\\d` is a WhatsApp video" — that judgment
should become a deterministic rule the engine (or a Q4 local model's cheap
pre-pass) applies next run WITHOUT a model call. This module turns accepted
judgments into a `[classify.name_patterns]` TOML block that `match_name_pattern`
consumes; it does NOT edit the user's mlo.toml — it emits a snippet the human
reviews and merges (the rule-diff gate). Pure; report.write_run_text does I/O.
"""
from __future__ import annotations

import os
import re

# The kinds match_name_pattern (and config validation) accept.
ALLOWED_KINDS = ("movie", "tv", "personal", "music", "junk")
_MIN_LITERAL = 4                     # a rule needs this many literal chars to be safe


def induce_prefix_pattern(names: list[str], min_literal: int = _MIN_LITERAL
                          ) -> str | None:
    """A conservative anchored regex generalizing basenames a critic judged the
    same way: the longest shared literal prefix, trailing digits generalized to
    `\\d`. None when the shared prefix is too short to be safe — the critic must
    then supply an explicit structural pattern rather than a rule that
    over-matches (the WhatsApp `^VID-\\d{8}-WA\\d` case)."""
    bases = [os.path.basename(n) for n in names if n]
    if not bases:
        return None
    lcp = os.path.commonprefix(bases)
    core = re.sub(r"\d+$", "", lcp)                  # drop the per-file digit run
    # The pattern anchors on the full shared prefix INCLUDING any separator
    # (so 'Wedding-01' -> '^Wedding\-\d', not '^Wedding\d' which matches
    # nothing). The literal-character count is the safety threshold.
    if len(re.sub(r"[^0-9A-Za-z]", "", core)) < min_literal:
        return None
    tail = r"\d" if len(lcp) > len(core) else ""
    return "^" + re.escape(core) + tail


def validate_rule(kind: str, regex: str) -> None:
    """Reject an unusable or dangerously-broad rule (raises ValueError)."""
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"kind {kind!r} not in {ALLOWED_KINDS}")
    if "'" in regex:
        raise ValueError("regex may not contain a single quote (TOML literal)")
    try:
        re.compile(regex)
    except re.error as e:
        raise ValueError(f"bad regex {regex!r}: {e}")
    literal = re.sub(r"[^0-9A-Za-z]", "", re.sub(r"\\.", "", regex))
    if len(literal) < _MIN_LITERAL:
        raise ValueError(
            f"regex {regex!r} has too few literal characters — it would "
            f"over-match; needs >= {_MIN_LITERAL}")


def distill(judgments: list[dict]) -> dict:
    """Group critic judgments into validated per-kind rules.

    judgments: [{filename, kind, pattern?}]. A judgment may carry an explicit
    structural `pattern` (the critic's own); otherwise a prefix pattern is
    induced from all same-kind filenames. Returns {rules, coverage, dropped}:
    `coverage` maps each rule to the judged filenames it actually matches (for
    the human to eyeball), `dropped` explains any judgment that yielded no safe
    rule."""
    by_kind: dict[str, list[dict]] = {}
    for j in judgments:
        by_kind.setdefault(j["kind"], []).append(j)

    rules: dict[str, list[str]] = {}
    coverage: dict[str, list[str]] = {}
    dropped: list[dict] = []
    for kind, group in by_kind.items():
        names = [j["filename"] for j in group]
        candidates: list[str] = []
        explicit = [j["pattern"] for j in group if j.get("pattern")]
        candidates.extend(dict.fromkeys(explicit))
        if not explicit:
            induced = induce_prefix_pattern(names)
            if induced:
                candidates.append(induced)
        kept: list[str] = []
        for rx in candidates:
            try:
                validate_rule(kind, rx)
            except ValueError as e:
                dropped.append({"kind": kind, "pattern": rx, "why": str(e)})
                continue
            matched = [n for n in names
                       if re.match(rx, os.path.basename(n), re.IGNORECASE)]
            if not matched:
                dropped.append({"kind": kind, "pattern": rx,
                                "why": "matched none of its own examples"})
                continue
            kept.append(rx)
            coverage[rx] = matched
        if kept:
            rules[kind] = kept
        else:
            dropped.append({"kind": kind, "pattern": None,
                            "why": f"no safe rule for {len(names)} example(s)"})
    return {"rules": rules, "coverage": coverage, "dropped": dropped}


def render_patterns_toml(rules: dict[str, list[str]]) -> str:
    """Render validated rules as a mergeable `[classify.name_patterns]` block
    (TOML literal strings, so regex backslashes survive verbatim)."""
    lines = ["[classify.name_patterns]",
             "# Distilled from accepted critic judgments — review before merging."]
    for kind in sorted(rules):
        arr = ", ".join("'" + p + "'" for p in rules[kind])
        lines.append(f"{kind} = [{arr}]")
    return "\n".join(lines) + "\n"
