"""plan flatten-provenance — strip device-origin path segments (E_NAS1,
G_Phone1, HDD2_Part2 …) from indexed files. The C27 mechanism.

Contract: segment-1 scoping, C21 dedup guard, L17 collision detection,
exclude_srcs cross-plan discipline, L12 protected-path refusal, idempotence.
"""
from __future__ import annotations

import os

from conftest import make_file
from helpers import make_cfg
from mlo import fingerprint, plan as planmod
from mlo.apply import apply_plan
from mlo.plan import PlanError
from mlo.report import read_plan

TAX = {"Video": (".mp4", ".mkv"), "Audio": (".mp3",),
       "Photos": (".jpg",), "Documents": (".pdf", ".txt")}


def seed_library(world, rels: list[str], content_by_rel=None) -> None:
    st = world["store"]
    for rel in rels:
        payload = (content_by_rel or {}).get(rel, rel.encode() * 3)
        p = make_file(world["lib"] / rel, payload)
        size, qh = fingerprint.quick(str(p))
        st.index_upsert(rel.replace("/", os.sep), size, qh,
                        os.stat(p).st_mtime_ns, "seed")
    st.index_commit()
    run = st.start_run("seed", [], "cfg-test", "t")
    st.artifact_register("index:library", "index",
                         {"root": str(world["lib"])}, "cfg-test", run)


def n(*segs):
    return os.sep.join(segs)


def test_happy_path_strips_provenance_segment(world):
    """The core case: a file under a drive-letter provenance folder gets its
    segment-1 dropped. Nested structure below is preserved."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Documents/E_NAS1/taxes.pdf",
                         "Documents/E_HDD2_Part1/C/Desktop/note.txt",
                         "Backups/G_Phone2/phone.zip"])
    st = world["store"]

    res = planmod.build_flatten_provenance(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    lib = cfg.library_root

    dests = {r["dst"] for r in rows}
    assert os.path.join(lib, "Documents", "taxes.pdf") in dests
    assert os.path.join(lib, "Documents", "C", "Desktop", "note.txt") in dests
    assert os.path.join(lib, "Backups", "phone.zip") in dests
    assert res.n_rows == 3
    # rules carry the segment so clustering splits per provenance folder
    rules = {r["reason"]["rule"] for r in rows}
    assert "flatten:provenance:E_NAS1" in rules
    assert "flatten:provenance:G_Phone2" in rules


def test_media_bucket_files_stay_put(world):
    """C28: Audio/Video/Videos/Photos/Images tops are the media taxonomy's
    territory. A provenance-folder file inside them stays put — reorganize/
    audio-triage decisions apply, not flat-strip laundering. The signal that
    'my audio patterns are incomplete' must not be erased."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Audio/G_Phone2/abhang govindha.3ga",
                         "Photos/E_NAS1/family.jpg",
                         "Documents/E_NAS1/taxes.pdf"])
    st = world["store"]

    res = planmod.build_flatten_provenance(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    lib = cfg.library_root

    dests = {r["dst"] for r in rows}
    srcs = {r["src"] for r in rows}
    # only the Documents file moves; Audio and Photos stay put
    assert dests == {os.path.join(lib, "Documents", "taxes.pdf")}
    assert res.n_rows == 1
    assert not any("Audio" in s for s in srcs)
    assert not any("Photos" in s for s in srcs)
    assert any("media bucket (stay put): 2" in n_ for n_ in res.notes)


def test_execute_then_converge_to_zero(world):
    """Idempotence: after execute, re-running the builder yields 0 rows."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Documents/E_NAS1/only.pdf"])
    st = world["store"]

    res = planmod.build_flatten_provenance(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 1
    r = apply_plan(st, cfg, res.path,
                   st.start_run("x", [], cfg.config_hash, "t"),
                   execute=True, drive_of=world["drive_of"])
    assert r.exit_code == 0

    res2 = planmod.build_flatten_provenance(st, cfg,
                                            drive_of=world["drive_of"])
    assert res2.n_rows == 0


def test_collision_stays_put(world):
    """Two provenance folders holding same-named files collide on the flattened
    destination — L17 says never resolve by naming. Both stay put."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world,
                 ["Documents/E_NAS1/readme.txt",
                  "Documents/I_SSD1/readme.txt"],
                 content_by_rel={
                     "Documents/E_NAS1/readme.txt": b"A",
                     "Documents/I_SSD1/readme.txt": b"B"})
    st = world["store"]

    res = planmod.build_flatten_provenance(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 0
    assert any("collisions (stay put): 2" in n_ for n_ in res.notes)


def test_dest_occupied_stays_put(world):
    """A flatten dest that already exists in the index (e.g. someone already
    put a canonical copy alongside the provenance one) is skipped."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Documents/E_NAS1/report.pdf",
                         "Documents/report.pdf"],
                 content_by_rel={"Documents/E_NAS1/report.pdf": b"A",
                                 "Documents/report.pdf": b"B"})
    st = world["store"]
    res = planmod.build_flatten_provenance(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 0
    assert any("collisions (stay put): 1" in n_ for n_ in res.notes)


def test_c21_twin_skip(world):
    """Files with a fingerprint twin ANYWHERE in the library stay put — dedup
    decision, not placement (C21). This mirrors reorganize's precedent."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Documents/E_NAS1/thesis.pdf",
                         "Documents/twin_copy.pdf"],
                 content_by_rel={"Documents/E_NAS1/thesis.pdf": b"SAME",
                                 "Documents/twin_copy.pdf": b"SAME"})
    st = world["store"]
    res = planmod.build_flatten_provenance(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 0
    assert any("duplicate content (stay put): 1" in n_ for n_ in res.notes)


def test_junk_stays_put(world):
    """Zero-byte / named-junk files are never re-homed by flatten — they're for
    the dedup/staging path (date-drain zero-byte precedent)."""
    cfg = make_cfg(world, taxonomy=TAX)
    st = world["store"]
    # zero-byte
    p = make_file(world["lib"] / "Documents/E_NAS1/empty.pdf", b"")
    size, qh = fingerprint.quick(str(p))
    st.index_upsert(n("Documents", "E_NAS1", "empty.pdf"),
                    size, qh, os.stat(p).st_mtime_ns, "seed")
    # named junk
    p2 = make_file(world["lib"] / "Documents/E_NAS1/Thumbs.db", b"xx")
    size, qh = fingerprint.quick(str(p2))
    st.index_upsert(n("Documents", "E_NAS1", "Thumbs.db"),
                    size, qh, os.stat(p2).st_mtime_ns, "seed")
    st.index_commit()
    run = st.start_run("seed", [], "cfg-test", "t")
    st.artifact_register("index:library", "index",
                         {"root": str(world["lib"])}, "cfg-test", run)

    res = planmod.build_flatten_provenance(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 0
    assert any("junk (stay put): 2" in n_ for n_ in res.notes)


def test_exclude_srcs_prevents_double_planning(world):
    """A src already claimed by another section this pilot run must not appear
    in flatten's plan — the pilot passes reorganize/date-drain/dedup-library
    srcs. Live-library trigger: date-drain owns Photos\\E_NAS1\\setup.bmp."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Documents/E_NAS1/a.pdf",
                         "Documents/E_NAS1/b.pdf"])
    st = world["store"]
    claimed = os.path.join(cfg.library_root, "Documents", "E_NAS1", "a.pdf")
    res = planmod.build_flatten_provenance(
        st, cfg, exclude_srcs={claimed}, drive_of=world["drive_of"])
    assert res.n_rows == 1
    _, rows, _ = read_plan(res.path)
    assert rows[0]["src"] == os.path.join(cfg.library_root, "Documents",
                                          "E_NAS1", "b.pdf")
    assert any("excluded (claimed by prior sections): 1" in n_
               for n_ in res.notes)


def test_under_scoping(world):
    """--under narrows examination to the given top-level prefixes."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Documents/E_NAS1/a.pdf",
                         "Backups/G_Phone2/b.zip"])
    st = world["store"]

    res = planmod.build_flatten_provenance(st, cfg, under=["Documents"],
                                           drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 1
    assert "Documents" in rows[0]["src"]
    assert "Backups" not in rows[0]["src"]


def test_files_without_provenance_segment_stay_put(world):
    """Only segment index 1 is checked. A canonical file (segment 1 = 'Photos',
    say) or a shallow file (2 segments) is untouched."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Documents/Photos/holiday.jpg",
                         "Documents/at-root.pdf"])
    st = world["store"]
    res = planmod.build_flatten_provenance(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 0


def test_c47_provenance_deep_inside_curated_tree_is_stripped(world):
    """C47: a provenance segment sitting DEEPER inside a curated layout root
    (music_root/<genre>/<PROV>/...) is stripped — the file is already
    triaged into the curated subtree, and the device folder is a proven
    interloper with an unambiguous de-provenanced parent."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Audio/Music/Classical/E_NAS1/x.mp3"])
    st = world["store"]
    res = planmod.build_flatten_provenance(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 1
    assert rows[0]["dst"] == os.path.join(
        cfg.library_root, "Audio", "Music", "Classical", "x.mp3")
    assert rows[0]["reason"]["rule"] == "flatten:provenance:E_NAS1"


def test_c47_media_top_direct_child_still_not_stripped(world):
    """The C28 boundary holds: a provenance segment that is the media-bucket
    TOP's direct child (not inside any curated layout root) is NOT stripped
    — audio/photo-triage's gap, not flatten's to launder."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Audio/I_SSD1/song.mp3"])
    st = world["store"]
    res = planmod.build_flatten_provenance(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 0
    assert any("media bucket (stay put): 1" in n_ for n_ in res.notes)


def test_c47_personal_video_provenance_not_stripped_in_place(world):
    """Defect fix (2026-07-15): personal_root is EXCLUDED from the C47
    deeper-strip set. A provenance folder inside Video\\Personal (unlike
    Audio\\Music) is NOT flattened in place — it is C45 date-drain /
    Undated-shelf territory. This was the live bug: ~1,120 video rows were
    being flattened instead of dated/shelved."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Video/Personal/HDD2_Part2/FILE043.mp4"])
    st = world["store"]
    res = planmod.build_flatten_provenance(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 0
    assert any("media bucket (stay put): 1" in n_ for n_ in res.notes)


def test_protected_path_refuses_whole_plan(world):
    """L12: a plan touching a protected substring refuses to build. A whole-plan
    refusal is by design — the operator fixes the config or the layout first."""
    # bluestacks is a protected substring (helpers.make_cfg)
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Documents/E_bluestacks_drop/file.pdf"])
    st = world["store"]
    try:
        planmod.build_flatten_provenance(st, cfg, drive_of=world["drive_of"])
    except PlanError as e:
        assert "protected" in str(e).lower()
    else:
        raise AssertionError("expected PlanError for protected path")
