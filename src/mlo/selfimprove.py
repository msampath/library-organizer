"""The self-improving eval loop (eval-and-self-improvement.md / W4) — the capstone.

Dry-run only. It closes the gap between where files ARE and where they SHOULD be
by distilling each fix into a RULE the deterministic engine (or a Q4 local
model's cheap pre-pass) applies next time — measured, gated, and never touching a
single file. It mutates classification RULES only; the kernel never runs in it.

Two failure modes are HARD STOPS (§7 safety invariants):
  - a DANGEROUS error (a keeper classified for disposal), and
  - a REGRESSION (a solved past failure reintroduced),
either of which stops the loop and escalates to the human — no auto-proceed on a
safety failure, ever. Fixes are distilled rules, never test edits. The converged
rule-diff is returned for HUMAN APPROVAL before it is ever real.

Convergence under a cheap/deterministic classifier IS the evidence for the
Q4-fidelity thesis: the system is strong enough that simple judgment suffices.
"""
from __future__ import annotations

import dataclasses
import json
import os

from . import distill
from .agent.tasks import match_name_pattern
from .config import Config

# Every way the system can be wrong, enumerated so nothing fails silently (E6).
FAILURE_MODES = ("unmatched", "misclassified", "overconfident", "underconfident",
                 "dangerous", "drift", "connector_fail", "regression",
                 "provenance_miss")
DISPOSAL_KINDS = {"junk"}
KEEPER_KINDS = {"movie", "tv", "personal", "music"}


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _classify_kind(cfg: Config, filename: str) -> str | None:
    m = match_name_pattern(cfg, filename)
    return m[0] if m else None


def score(cfg: Config, fixtures: list[dict],
          known_failures: list[dict]) -> dict:
    """Score the deterministic classifier against labeled fixtures. Returns the
    scorecard with a full failure_modes block (E4/E6). `dangerous` and
    `regression` are the hard-gate counters."""
    modes = {m: 0 for m in FAILURE_MODES}
    decided = correct = 0
    wrong: list[dict] = []
    for fx in fixtures:
        expect = fx["expect_kind"]
        got = _classify_kind(cfg, _basename(fx["item"]))
        if got is None:
            modes["unmatched"] += 1
            wrong.append({**fx, "got": None})
            continue
        decided += 1
        if got == expect:
            correct += 1
            continue
        modes["misclassified"] += 1
        if got in DISPOSAL_KINDS and expect in KEEPER_KINDS:
            modes["dangerous"] += 1                 # a keeper marked for disposal
        wrong.append({**fx, "got": got})
    for kf in known_failures:
        got = _classify_kind(cfg, _basename(kf["item"]))
        if got is not None and got == kf.get("bad_kind"):
            modes["regression"] += 1                # a solved failure reintroduced
    return {"total": len(fixtures), "decided": decided, "correct": correct,
            "accuracy_on_decided": round(correct / decided, 3) if decided else None,
            "failure_modes": modes, "wrong": wrong}


def _propose_rules(wrong: list[dict]) -> dict[str, list[str]]:
    """Distill fixes for the misclassified items into per-kind name-pattern
    rules (never a test edit — a RULE the engine will apply)."""
    judgments = []
    for w in wrong:
        kind = w["expect_kind"]
        if kind not in distill.ALLOWED_KINDS:
            continue                                # not a name-pattern surface
        judgments.append({"filename": _basename(w["item"]), "kind": kind,
                          "pattern": w.get("pattern")})
    return distill.distill(judgments)["rules"] if judgments else {}


def _merge(a: dict[str, list[str]], b: dict[str, list[str]]) -> dict[str, list[str]]:
    out = {k: list(v) for k, v in a.items()}
    for k, v in b.items():
        out[k] = list(dict.fromkeys(out.get(k, []) + list(v)))
    return out


def _with_rules(cfg: Config, rules: dict[str, list[str]]) -> Config:
    """A config clone whose name_patterns include the distilled rules (existing
    config patterns preserved). Pure — no disk, no library."""
    merged = _merge({k: list(v) for k, v in cfg.name_patterns.items()}, rules)
    return dataclasses.replace(
        cfg, name_patterns={k: tuple(v) for k, v in merged.items()})


def _safe(sc: dict) -> bool:
    fm = sc["failure_modes"]
    return fm["dangerous"] == 0 and fm["regression"] == 0


def improve(cfg: Config, fixtures: list[dict], known_failures: list[dict],
            max_rounds: int = 5) -> dict:
    """Run the loop: score -> gate -> diagnose -> distill -> apply scratch rules
    -> re-score -> keep iff improved AND safe -> converge. Returns the rule-diff
    (for human approval), before/after scorecards, and per-round history. Never
    touches the library."""
    base = score(cfg, fixtures, known_failures)
    if not _safe(base):
        return {"status": "halted", "reason": "dangerous or regression in the "
                "starting state — fix the seed rules first, do not proceed",
                "before": base, "after": base, "rounds": [], "rules": {}}

    applied: dict[str, list[str]] = {}
    cur = cfg
    rounds: list[dict] = []
    for rnd in range(max_rounds):
        sc = score(cur, fixtures, known_failures)
        if not sc["wrong"]:
            break                                   # converged
        proposed = _propose_rules(sc["wrong"])
        if not proposed:
            rounds.append({"round": rnd, "note": "no distillable rule for the "
                           "remaining misclassifications"})
            break
        trial_rules = _merge(applied, proposed)
        trial_cfg = _with_rules(cur, trial_rules)
        trial = score(trial_cfg, fixtures, known_failures)
        # keep the diff only if it decides more correctly AND stays safe
        if trial["correct"] > sc["correct"] and _safe(trial):
            applied, cur = trial_rules, trial_cfg
            rounds.append({"round": rnd, "correct": f"{sc['correct']}->"
                           f"{trial['correct']}", "rules_added": proposed})
        else:
            rounds.append({"round": rnd, "rejected": proposed,
                           "why": "no gain" if trial["correct"] <= sc["correct"]
                           else "would introduce a dangerous error or regression"})
            break

    final = score(cur, fixtures, known_failures)
    if not _safe(final):
        status = "halted"
    elif not final["wrong"]:
        status = "converged"
    elif final["correct"] > base["correct"]:
        status = "improved"
    else:
        status = "no_safe_fix"          # a proposed diff was rejected as unsafe
    return {"status": status, "before": base, "after": final,
            "rounds": rounds, "rules": applied}


# ── fixture / corpus loaders ─────────────────────────────────────────────────

def load_fixtures(directory: str) -> list[dict]:
    """Merge every evals/dogfood/*.json list into one fixture list."""
    import glob
    out: list[dict] = []
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            out.extend(data)
    return out


def load_known_failures(path: str) -> list[dict]:
    """Load evals/known-failures.jsonl (one bad->good record per line)."""
    out: list[dict] = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
