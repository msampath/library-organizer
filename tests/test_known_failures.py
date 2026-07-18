"""E3: the shipped deterministic classifier must reintroduce NO historical
misclassification. This test fails the moment a rule change re-commits a solved
mistake — the regression guard the defect-ledger contract demands."""
from __future__ import annotations

import os

from helpers import make_cfg
from mlo import selfimprove
from mlo.agent.tasks import match_name_pattern

_KNOWN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "evals", "known-failures.jsonl")


def test_shipped_classifier_reintroduces_no_known_failure(world):
    cfg = make_cfg(world)                          # default name_patterns + built-ins
    failures = selfimprove.load_known_failures(_KNOWN)
    assert failures, "known-failures corpus must not be empty"
    for kf in failures:
        base = kf["item"].replace("\\", "/").rsplit("/", 1)[-1]
        got = match_name_pattern(cfg, base)
        got_kind = got[0] if got else None
        assert got_kind != kf["bad_kind"], f"regressed: {kf['failure']}"
