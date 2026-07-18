"""Plan-build gates: freshness, ordering, coverage, unique destinations."""
from __future__ import annotations

import os

import pytest

from helpers_plan import make_cfg, seed_source, seed_store
from mlo import plan as planmod
from mlo.plan import CoverageBlockedError, OrderingError, PlanError
from mlo.verdict import StaleArtifactError


def organize_ready(world, files=None, verdicts=None):
    cfg = make_cfg(world)
    files = files or {"a/movie.mp4": b"M" * 2048, "a/song.mp3": b"S" * 512,
                      "junk/thumbs.db": b"T"}
    pre = seed_source(world, cfg, files)
    verdicts = verdicts or {
        "a/movie.mp4": ("UNIQUE", "bucket:Video"),
        "a/song.mp3": ("UNIQUE", "bucket:Audio"),
        "junk/thumbs.db": ("JUNK", "junk:name"),
    }
    seed_store(world, cfg, pre, verdicts)
    return cfg


def test_stale_artifact_refused_with_remedy(world):
    cfg = organize_ready(world)
    world["store"].artifact_set_status("verdicts:eSrc", "stale")
    with pytest.raises(StaleArtifactError, match="mlo verdicts eSrc"):
        planmod.build_organize(world["store"], cfg, "eSrc")


def test_organize_builds_bucketed_unique_rows(world):
    cfg = organize_ready(world)
    res = planmod.build_organize(world["store"], cfg, "eSrc")
    assert res.n_rows == 2
    from mlo.report import read_plan
    header, rows, plan_id = read_plan(res.path)
    assert plan_id == res.plan_id
    dsts = {r["dst"] for r in rows}
    assert os.path.join(cfg.library_root, "Video", "eSrc", "a", "movie.mp4") in dsts
    assert all(r["kind"] == "copy_in" for r in rows)
    assert world["store"].artifact_get(f"plan:{res.plan_id}").status == "fresh"


def test_coverage_blocks_organize(world):
    cfg = make_cfg(world, max_unmatched_pct=5.0)
    files = {f"weird/file{i}.xyzext": b"x" * (i + 1) for i in range(20)}
    files["ok/song.mp3"] = b"S" * 128
    pre = seed_source(world, cfg, files)
    verdicts = {rel: ("REVIEW", "no-rule-matched") for rel in files}
    verdicts["ok/song.mp3"] = ("UNIQUE", "bucket:Audio")
    seed_store(world, cfg, pre, verdicts)
    with pytest.raises(CoverageBlockedError, match="xyzext"):
        planmod.build_organize(world["store"], cfg, "eSrc")


def test_dedup_requires_organize_first(world):
    cfg = organize_ready(world)
    with pytest.raises(OrderingError, match="copy before stage"):
        planmod.build_dedup(world["store"], cfg, "eSrc",
                            drive_of=world["drive_of"])


def test_dedup_after_organize_executed(world):
    cfg = organize_ready(world)
    res = planmod.build_organize(world["store"], cfg, "eSrc")
    world["store"].artifact_set_status(f"plan:{res.plan_id}", "executed")
    ded = planmod.build_dedup(world["store"], cfg, "eSrc",
                              drive_of=world["drive_of"])
    assert ded.n_rows == 1                      # the JUNK row
    from mlo.report import read_plan
    _, rows, _ = read_plan(ded.path)
    assert rows[0]["kind"] == "stage_move"
    assert rows[0]["dst"].startswith(os.path.join(str(world["E"]), "Delete", "eSrc"))


def test_dedup_waiver_is_explicit_and_noted(world):
    cfg = organize_ready(world)
    ded = planmod.build_dedup(world["store"], cfg, "eSrc", waive_organize=True,
                              drive_of=world["drive_of"])
    assert any("WAIVED" in n for n in ded.notes)


def test_dedup_confirm_bytes_keeps_middle_differing_file(world):
    """--confirm-mb re-checks each ORGANIZED file against its library twin at a
    larger head+tail before staging it OUT. A file that matches the 128K quick
    screen but DIFFERS in the middle (identical size + first/last 128K, unique
    middle) must be kept in place, never swept off its only unique content."""
    from pathlib import Path

    from conftest import make_file
    from mlo import fingerprint
    from mlo.report import read_plan

    cfg = make_cfg(world)
    src_root, lib_root = Path(cfg.sources[0].root), Path(cfg.library_root)
    CH, MID = 128 * 1024, 1024 * 1024
    # (a) quick(128K) matches its twin, but the 1 MiB region differs in the middle
    src_a = make_file(src_root / "a.mkv", b"H" * CH + b"A" * MID + b"1" * CH)
    lib_a = make_file(lib_root / "libA.mkv", b"H" * CH + b"B" * MID + b"1" * CH)
    # (b) a genuine byte-identical twin
    ident = b"K" * CH + b"C" * MID + b"2" * CH
    src_b = make_file(src_root / "b.mkv", ident)
    make_file(lib_root / "libB.mkv", ident)

    qa, qb = fingerprint.quick(str(src_a)), fingerprint.quick(str(src_b))
    assert qa == fingerprint.quick(str(lib_a))                 # 128K screen: same
    assert fingerprint.region(str(src_a), MID) != \
        fingerprint.region(str(lib_a), MID)                   # 1 MiB: different

    seed_store(world, cfg,
               {"a.mkv": qa, "b.mkv": qb},
               {"a.mkv": ("ORGANIZED", "fp:lib"), "b.mkv": ("ORGANIZED", "fp:lib")},
               {"libA.mkv": qa, "libB.mkv": qb})

    res = planmod.build_dedup(world["store"], cfg, "eSrc", waive_organize=True,
                              drive_of=world["drive_of"], confirm_bytes=MID)
    _, rows, _ = read_plan(res.path)
    staged = {os.path.basename(r["src"]) for r in rows}
    assert staged == {"b.mkv"}                                 # only the true twin
    assert res.n_rows == 1
    assert any("failed confirm" in n for n in res.notes)


def test_dedup_missing_staging_root_is_plan_error(world):
    cfg = organize_ready(world)
    world["store"].artifact_set_status("verdicts:eSrc", "fresh")
    with pytest.raises(PlanError, match=r"no \[staging\] root"):
        planmod.build_dedup(world["store"], cfg, "eSrc", waive_organize=True,
                            drive_of=lambda p: "Q")   # a drive with no staging


def test_organize_same_basename_collision_falls_back_flat(world):
    """Review C16: two different photos both named IMG_0001.jpg are ordinary
    data — they must not refuse the whole plan. The colliding pair demotes to
    provenance-flat destinations; everything still gets organized."""
    cfg = make_cfg(world)
    files = {"phone-a/DCIM/IMG_0001.mp4": b"A" * 300,
             "phone-b/DCIM/IMG_0001.mp4": b"B" * 300,
             "films/Roja (1992).mp4": b"R" * 300}
    pre = seed_source(world, cfg, files)
    # same parent-dir grouping + same basename -> identical routed dests
    from mlo.taxonomy import Hints
    hints = {rel.replace("/", os.sep): Hints(media_kind="personal")
             for rel in files if "DCIM" in rel}
    seed_store(world, cfg, pre,
               {rel: ("UNIQUE", "bucket:Video") for rel in files})
    res = planmod.build_organize(world["store"], cfg, "eSrc",
                                 drive_of=world["drive_of"], hints=hints)
    assert res.n_rows == 3                        # nothing refused
    assert any("collided" in n for n in res.notes)
    from mlo.report import read_plan
    _, rows, _ = read_plan(res.path)
    flat = [r for r in rows if "+flat:collision" in r["reason"]["rule"]]
    assert len(flat) == 2                         # the colliding pair demoted
    assert all(os.path.join("Video", "eSrc") in r["dst"] for r in flat)


def test_duplicate_destination_rejected(world):
    rows = [
        {"src": "a", "dst": os.path.join("x", "same.bin")},
        {"src": "b", "dst": os.path.join("x", "SAME.bin") if os.name == "nt"
                     else os.path.join("x", "same.bin")},
    ]
    with pytest.raises(PlanError, match="duplicate destination"):
        planmod._rows_unique_dsts(rows)
