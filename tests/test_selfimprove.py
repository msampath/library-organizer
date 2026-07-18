"""The self-improving loop (E5): diagnose -> distil -> fix on re-run; the
dangerous-error and regression HARD STOPS; determinism (rules only)."""
from __future__ import annotations

import dataclasses

from helpers import make_cfg
from mlo import selfimprove


def _cfg(world, **name_patterns):
    cfg = make_cfg(world)
    return dataclasses.replace(
        cfg, name_patterns={k: tuple(v) for k, v in name_patterns.items()})


def test_loop_diagnoses_and_fixes_with_a_distilled_rule(world):
    """A novel junk convention that no built-in pattern catches is diagnosed, a
    rule is distilled, and it fixes every fixture on re-run — rules only."""
    cfg = _cfg(world)
    fixtures = [{"item": f"AcmeCorpPromo-{i}.mp4", "expect_kind": "junk"}
                for i in range(3)]
    out = selfimprove.improve(cfg, fixtures, [])
    assert out["before"]["correct"] == 0        # nothing matched at the start
    assert out["status"] == "converged"
    assert out["after"]["correct"] == 3
    assert "junk" in out["rules"]
    fm = out["after"]["failure_modes"]
    assert fm["dangerous"] == 0 and fm["regression"] == 0
    assert set(fm) == set(selfimprove.FAILURE_MODES)   # all modes observable (E6)


def test_loop_halts_on_a_dangerous_starting_state(world):
    """A seed rule that would junk a keeper is a HARD STOP before any round."""
    cfg = _cfg(world, junk=[r"^Appa"])           # would junk 'Appa 60th Birthday'
    fixtures = [{"item": "Appa 60th Birthday.mp4", "expect_kind": "personal"}]
    out = selfimprove.improve(cfg, fixtures, [])
    assert out["status"] == "halted"
    assert out["after"]["failure_modes"]["dangerous"] == 1


def test_loop_rejects_a_rule_that_would_regress(world):
    """A distilled rule that would reclassify a known-good item into its
    historical bad kind is rejected — safety over accuracy."""
    cfg = _cfg(world)
    fixtures = [{"item": "Wedding-01.mp4", "expect_kind": "junk"},
                {"item": "Wedding-02.mp4", "expect_kind": "junk"}]
    known = [{"failure": "family-video-not-junk",
              "item": "Wedding-99 Grandpa.mp4", "bad_kind": "junk"}]
    out = selfimprove.improve(cfg, fixtures, known)
    assert out["status"] == "no_safe_fix"        # the fix would regress -> refused
    assert out["rules"] == {}                     # nothing kept
    assert out["after"]["failure_modes"]["regression"] == 0   # known-good safe


def test_loop_is_pure_and_repeatable(world):
    """The loop takes no store and has no side effects: identical inputs give an
    identical result (it edits the map, never the territory)."""
    cfg = _cfg(world)
    fx = [{"item": f"AcmeCorpPromo-{i}.mp4", "expect_kind": "junk"}
          for i in range(3)]
    a = selfimprove.improve(cfg, fx, [])
    b = selfimprove.improve(cfg, fx, [])
    assert a["rules"] == b["rules"] and a["status"] == b["status"]
