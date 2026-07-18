"""The reliability protocol (docs/agent-design.md §3): what makes a small local
model trustworthy.

  parse -> validate -> ONE repair attempt (validator error shown to the model)
        -> escalate tier -> UNSURE/human.

Abstention is a first-class answer; malformed output can never leave this
module. Self-consistency (N-vote) buys accuracy back where tokens are free.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from .llm import ChainClient, ChainExhausted

UNSURE = "UNSURE"


class SchemaError(ValueError):
    """The reply failed validation — shown verbatim to the model on repair."""


@dataclass
class TaskSpec:
    """A bounded task: prompt template + schema validator + option space."""
    name: str
    system: str
    tier: str = "any"                       # 'any' | 'strong'
    max_tokens: int = 2048
    validate: Callable[[dict], dict] = lambda d: d   # raises SchemaError


@dataclass
class TaskOutcome:
    value: dict | None                      # validated payload, or None
    unsure: bool
    escalated: bool
    attempts: int
    ledger: list[dict] = field(default_factory=list)


def extract_json(text: str) -> dict:
    """Models wrap JSON in prose/fences; take the outermost object, strictly."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise SchemaError("no JSON object found in reply")
        candidate = text[start:end + 1]
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError as e:
        raise SchemaError(f"invalid JSON: {e}")
    if not isinstance(obj, dict):
        raise SchemaError("top-level JSON value must be an object")
    return obj


def run_task(client: ChainClient, spec: TaskSpec, user: str) -> TaskOutcome:
    """One task invocation with bounded repair and tier escalation."""
    ledger: list[dict] = []
    attempts = 0
    last_schema_err: SchemaError | None = None
    attempted_strong = False          # honesty for the give-up rollup (B-072)
    for tier, escalated in ((spec.tier, False), ("strong", True)):
        if escalated and spec.tier == "strong":
            break                            # nowhere higher to go
        if escalated and not getattr(client, "has_local", lambda: True)():
            # 'strong' differs from the base tier only by SKIPPING local
            # entries — on a chain with no enabled local it would re-buy the
            # identical cloud model (validator-B repro: 4 paid calls to the
            # same model, repair context discarded). Nowhere different to
            # go: give up honestly instead.
            break
        if escalated:
            attempted_strong = True
        # An escalated pass starts from the last validation failure, not a
        # blank slate — the strong model should know what the weak one got
        # wrong.
        prompt = user if last_schema_err is None else (
            f"{user}\n\nYour previous reply failed validation: "
            f"{last_schema_err}\nReply with ONLY the corrected JSON object.")
        for attempt in ("first", "repair"):
            attempts += 1
            try:
                reply = client.complete(spec.system, prompt, tier=tier,
                                        max_tokens=spec.max_tokens)
            except ChainExhausted as e:
                ledger.append({"tier": tier, "attempt": attempt,
                               "outcome": f"chain-exhausted: {e}"})
                break                        # try escalation (or give up)
            ledger.extend(reply.ledger)
            try:
                value = spec.validate(extract_json(reply.text))
                ledger.append({"tier": tier, "attempt": attempt, "outcome": "ok",
                               "entry": reply.entry})
                return TaskOutcome(value, unsure=False, escalated=escalated,
                                   attempts=attempts, ledger=ledger)
            except SchemaError as e:
                last_schema_err = e
                ledger.append({"tier": tier, "attempt": attempt,
                               "outcome": f"schema: {e}"})
                prompt = (f"{user}\n\nYour previous reply failed validation: "
                          f"{e}\nReply with ONLY the corrected JSON object.")
    # escalated reports what actually HAPPENED: a give-up that never ran a
    # strong-tier pass (no local chain, strong base tier) must not inflate
    # the escalation rollups in the ledger/evals (super-review B-072).
    return TaskOutcome(None, unsure=True, escalated=attempted_strong,
                       attempts=attempts, ledger=ledger)


def majority_vote(client: ChainClient, spec: TaskSpec, user: str,
                  key: Callable[[dict], str], n: int = 3) -> TaskOutcome:
    """Self-consistency for cheap local calls: N samples, majority answer wins;
    no majority -> UNSURE (an honest tie is an abstention, not a coin flip)."""
    outcomes = [run_task(client, spec, user) for _ in range(n)]
    answers = [key(o.value) for o in outcomes if o.value is not None]
    ledger = [r for o in outcomes for r in o.ledger]
    attempts = sum(o.attempts for o in outcomes)
    if not answers:
        return TaskOutcome(None, True, any(o.escalated for o in outcomes),
                           attempts, ledger)
    top, count = Counter(answers).most_common(1)[0]
    if count * 2 <= len(answers):            # no strict majority
        return TaskOutcome(None, True, False, attempts, ledger)
    winner = next(o for o in outcomes
                  if o.value is not None and key(o.value) == top)
    return TaskOutcome(winner.value, False, winner.escalated, attempts, ledger)


# ── shared validator helpers ────────────────────────────────────────────────

def require_keys(obj: dict, keys: tuple[str, ...]) -> None:
    missing = [k for k in keys if k not in obj]
    if missing:
        raise SchemaError(f"missing keys: {', '.join(missing)}")


def require_choice(value, options, what: str) -> None:
    if value not in options:
        raise SchemaError(
            f"{what} must be one of {sorted(options)!r}, got {value!r}")


def require_confidence(obj: dict) -> float:
    c = obj.get("confidence")
    if not isinstance(c, (int, float)) or not 0.0 <= float(c) <= 1.0:
        raise SchemaError("confidence must be a number in [0,1]")
    return float(c)
