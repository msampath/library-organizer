"""Distillation writeback: a critic's recurring judgment becomes a config rule
the deterministic engine applies next run with NO model call."""
from __future__ import annotations

import pytest

from mlo import distill

MINIMAL_CONFIG = "[library]\nroot = 'X:/Organized'\n"


def test_induce_prefix_pattern():
    # the anchor includes the shared separator
    assert distill.induce_prefix_pattern(
        ["UnityAds-a.mp4", "UnityAds-bb.mp4", "UnityAds-ccc.mp4"]) == r"^UnityAds\-"
    # a per-file numeric suffix generalizes to \d (separator preserved)
    assert distill.induce_prefix_pattern(
        ["cache001.dat", "cache002.dat", "cache003.dat"]) == r"^cache\d"
    assert distill.induce_prefix_pattern(
        ["Wedding-01.mp4", "Wedding-02.mp4"]) == r"^Wedding\-\d"
    # too little shared literal -> None (critic must supply a structural pattern)
    assert distill.induce_prefix_pattern(["a1.mp4", "b2.mp4"]) is None


def test_validate_rule_rejects_broad_bad_and_wrong_kind():
    with pytest.raises(ValueError):
        distill.validate_rule("junk", r"^\d+")            # no literal characters
    with pytest.raises(ValueError):
        distill.validate_rule("nope", "^UnityAds")        # kind not allowed
    with pytest.raises(ValueError):
        distill.validate_rule("junk", "[")                # unparseable regex
    distill.validate_rule("junk", "^UnityAds")            # ok


def test_distilled_rule_applies_without_a_model_call(tmp_path):
    """A10 acceptance: three critic 'junk' calls on UnityAds-* become one rule;
    once merged, the engine classifies a NEW UnityAds file deterministically."""
    from mlo.agent.tasks import match_name_pattern
    from mlo.config import load

    judgments = [{"filename": f"UnityAds-{i}.mp4", "kind": "junk"}
                 for i in range(3)]
    out = distill.distill(judgments)
    assert out["rules"]["junk"] == [r"^UnityAds\-"]
    block = distill.render_patterns_toml(out["rules"])
    assert "[classify.name_patterns]" in block

    p = tmp_path / "mlo.toml"
    p.write_text(MINIMAL_CONFIG + "\n" + block, encoding="utf-8")
    cfg = load(str(p))
    # deterministic — no LLM in the loop:
    assert match_name_pattern(cfg, "UnityAds-brandnew.mp4") == (
        "junk", "pattern:config:junk")
    assert match_name_pattern(cfg, "Roja (1992).mkv") is None    # untouched


def test_distill_takes_an_explicit_structural_pattern():
    """The WhatsApp case: prefix induction can't capture it, so the critic
    supplies the structural regex; distill validates and keeps it."""
    judgments = [{"filename": "VID-20230101-WA0007.mp4", "kind": "personal",
                  "pattern": r"^VID-\d{8}-WA\d"}]
    out = distill.distill(judgments)
    assert out["rules"]["personal"] == [r"^VID-\d{8}-WA\d"]
    assert out["coverage"][r"^VID-\d{8}-WA\d"] == ["VID-20230101-WA0007.mp4"]


def test_render_toml_roundtrips_through_config(tmp_path):
    from mlo.agent.tasks import match_name_pattern
    from mlo.config import load

    rules = {"personal": [r"^VID-\d{8}-WA\d"], "junk": ["^UnityAds"]}
    block = distill.render_patterns_toml(rules)
    p = tmp_path / "mlo.toml"
    p.write_text(MINIMAL_CONFIG + "\n" + block, encoding="utf-8")
    cfg = load(str(p))
    assert match_name_pattern(cfg, "VID-20230101-WA0099.mp4")[0] == "personal"
    assert match_name_pattern(cfg, "UnityAds-x.mp4")[0] == "junk"


def test_unsafe_judgment_is_dropped_not_forced():
    """A judgment that yields no safe rule is reported as dropped, never turned
    into an over-matching rule."""
    out = distill.distill([{"filename": "x.mp4", "kind": "junk"}])
    assert "junk" not in out["rules"]
    assert out["dropped"]
