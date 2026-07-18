"""Verdicts: freshness gates (L7), precedence, provenance."""
from __future__ import annotations

import os

import pytest

from conftest import make_file
from helpers import make_cfg
from mlo import scan, verdict
from mlo.verdict import StaleArtifactError


def scanned_world(world, cfg):
    scan.scan_library(world["store"], cfg, "run-lib")
    scan.scan_source(world["store"], cfg, "e", "run-src")


def test_missing_scan_artifact_names_remedy(world):
    cfg = make_cfg(world)
    with pytest.raises(StaleArtifactError, match=r"mlo scan e"):
        verdict.assign(world["store"], cfg, "e", "run-v")


def test_missing_index_artifact_names_remedy(world):
    cfg = make_cfg(world)
    make_file(world["E"] / "x.mp3", b"x")
    scan.scan_source(world["store"], cfg, "e", "run-src")
    with pytest.raises(StaleArtifactError, match=r"mlo scan library"):
        verdict.assign(world["store"], cfg, "e", "run-v")


def test_stale_scan_refused(world):
    cfg = make_cfg(world)
    make_file(world["E"] / "x.mp3", b"x")
    scanned_world(world, cfg)
    world["store"].artifact_set_status("scan:e", "stale")
    with pytest.raises(StaleArtifactError, match="stale"):
        verdict.assign(world["store"], cfg, "e", "run-v")


def test_junk_beats_organized(world):
    cfg = make_cfg(world)
    empty = make_file(world["E"] / "zero.mp3", b"")
    scanned_world(world, cfg)
    # even with a matching index fingerprint, zero-byte junk wins
    from mlo import fingerprint
    size, qh = fingerprint.quick(str(empty))
    world["store"].index_upsert("Audio/zero.mp3", size, qh, 0, "s")
    world["store"].index_commit()
    counts = verdict.assign(world["store"], cfg, "e", "run-v")
    assert counts["JUNK"] == 1
    row = next(world["store"].source_iter("e", "JUNK"))
    assert row["verdict_rule"] == "junk:zero-byte"


def test_organized_by_fingerprint(world):
    cfg = make_cfg(world)
    dup = make_file(world["E"] / "dup.mp3", b"same content")
    make_file(world["lib"] / "Audio" / "already.mp3", b"same content")
    scanned_world(world, cfg)
    counts = verdict.assign(world["store"], cfg, "e", "run-v")
    assert counts["ORGANIZED"] == 1
    row = next(world["store"].source_iter("e", "ORGANIZED"))
    assert row["verdict_rule"] == "fp-match"
    assert dup.exists()


def test_unique_by_bucket_and_review_otherwise(world):
    cfg = make_cfg(world)
    make_file(world["E"] / "song.mp3", b"unique tune")
    make_file(world["E"] / "mystery.xyz", b"???")
    scanned_world(world, cfg)
    counts = verdict.assign(world["store"], cfg, "e", "run-v")
    assert counts["UNIQUE"] == 1 and counts["REVIEW"] == 1
    uq = next(world["store"].source_iter("e", "UNIQUE"))
    assert uq["verdict_rule"] == "bucket:Audio"
    rv = next(world["store"].source_iter("e", "REVIEW"))
    assert rv["verdict_rule"] == "no-rule-matched"


def test_counts_and_artifact(world):
    cfg = make_cfg(world)
    make_file(world["E"] / "a.mp3", b"a-tune")
    make_file(world["E"] / "thumbs.db", b"cache")
    make_file(world["E"] / "weird.blob", b"???")
    scanned_world(world, cfg)
    counts = verdict.assign(world["store"], cfg, "e", "run-v")
    assert counts == {"ORGANIZED": 0, "JUNK": 1, "UNIQUE": 1, "REVIEW": 1}
    assert world["store"].artifact_fresh("verdicts:e", cfg.config_hash)
    assert world["store"].source_verdict_counts("e") == {
        "JUNK": 1, "UNIQUE": 1, "REVIEW": 1}
