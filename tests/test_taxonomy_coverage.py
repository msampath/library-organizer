"""Taxonomy: totality, no implicit Other, blocking coverage (L4)."""
from __future__ import annotations

from hypothesis import given, strategies as st

from helpers import make_cfg
from mlo import taxonomy

_names = st.text(
    alphabet=st.characters(blacklist_characters="/\\\x00", min_codepoint=1),
    min_size=1, max_size=60)


def cfg_for_pure_tests(tmp_path):
    world = {"lib": tmp_path / "lib", "E": tmp_path / "E", "I": tmp_path / "I"}
    return make_cfg(world)


@given(name=_names, size=st.integers(min_value=0, max_value=10**12))
def test_classify_junk_is_total(name, size):
    cfg = make_cfg({"lib": "L", "E": "E", "I": "I"})
    out = taxonomy.classify_junk(cfg, name, size)
    assert out is None or isinstance(out, str)


@given(name=_names)
def test_bucket_labels_come_from_config_only(name):
    cfg = make_cfg({"lib": "L", "E": "E", "I": "I"})
    got = taxonomy.bucket_for(cfg, name)
    if got is not None:
        label, rule = got
        assert label in cfg.taxonomy
        assert rule.startswith("tax:ext:")


def test_no_implicit_other_bucket(tmp_path):
    cfg = cfg_for_pure_tests(tmp_path)
    assert taxonomy.bucket_for(cfg, "unclassified.zzz") is None
    assert taxonomy.bucket_for(cfg, "no_extension_at_all") is None


def test_coverage_blocks_above_threshold(tmp_path):
    cfg = cfg_for_pure_tests(tmp_path)          # threshold 5.0
    files = ["a.mp3"] + [f"blob{i}.dat" for i in range(9)]   # 90% unmatched
    cov = taxonomy.coverage(cfg, files)
    assert cov.blocked and cov.unmatched_count == 9
    assert cov.unmatched_pct == 90.0


def test_coverage_not_blocked_at_threshold_exactly(tmp_path):
    cfg = cfg_for_pure_tests(tmp_path)
    files = [f"m{i}.mp3" for i in range(95)] + [f"b{i}.dat" for i in range(5)]
    cov = taxonomy.coverage(cfg, files)
    assert cov.unmatched_pct == 5.0 and not cov.blocked


def test_missing_majority_keyword_names_itself(tmp_path):
    cfg = cfg_for_pure_tests(tmp_path)
    files = [f"Shows/English/episode_{i}_english.mkvv" for i in range(500)]
    files += ["ok.mp3"] * 10
    cov = taxonomy.coverage(cfg, files)
    assert cov.blocked
    tokens = [t for t, _ in cov.top_unmatched_tokens]
    assert "english" in tokens[:5]


def test_empty_input_not_blocked(tmp_path):
    cfg = cfg_for_pure_tests(tmp_path)
    cov = taxonomy.coverage(cfg, [])
    assert cov.total == 0 and cov.unmatched_pct == 0.0 and not cov.blocked


def test_extension_matching_case_insensitive(tmp_path):
    cfg = cfg_for_pure_tests(tmp_path)
    assert taxonomy.bucket_for(cfg, "LOUD.MP3") == ("Audio", "tax:ext:.mp3")
