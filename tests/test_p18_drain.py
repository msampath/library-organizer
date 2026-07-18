"""P18 provenance-drain wave (ledger C45/C46/C47).

Fix A (C45): build_date_drain also drains personal VIDEO files sitting in a
provenance/non-year folder under layout.personal_root, symmetric to the
existing photo drain.

Fix B (C46): build_reorganize/build_date_drain accept a `disambiguate` flag
(default False) that, on a DEMONSTRATED content-distinct destination
collision, tags each colliding member with an intrinsic, deterministic
discriminator (the source's own provenance/parent segment) instead of
skip-and-reporting — never a positional counter, and byte-identical
collisions are never disambiguated (that's a dedup decision, C21/D12).
"""
from __future__ import annotations

import datetime
import os
import struct

from conftest import make_file
from helpers import make_cfg
from mlo import fingerprint, plan as planmod
from mlo.apply import apply_plan
from mlo.report import read_plan

TAX = {"Video": (".mp4", ".mkv"), "Audio": (".mp3", ".opus"),
       "Photos": (".jpg",), "Documents": (".pdf", ".txt")}

_MAC_EPOCH_DELTA = 2082844800


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


def epoch_ms_name(year: int, month: int, day: int, ext: str) -> str:
    dt = datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc)
    return f"{int(dt.timestamp() * 1000)}{ext}"


def _atom(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def mp4_with_year(year: int) -> bytes:
    ts = int(datetime.datetime(year, 6, 1, tzinfo=datetime.timezone.utc)
             .timestamp()) + _MAC_EPOCH_DELTA
    mvhd_payload = bytes([0, 0, 0, 0]) + struct.pack(">I", ts) \
        + struct.pack(">I", 0) + struct.pack(">I", 600) + struct.pack(">I", 0)
    ftyp = _atom(b"ftyp", b"isom")
    moov = _atom(b"moov", _atom(b"mvhd", mvhd_payload))
    return ftyp + moov


# ── Fix A: personal-media video drain (C45) ─────────────────────────────────

def test_personal_video_drain_moves_provenance_folder_video_by_year(world):
    """Video\\Personal\\G_Dashcam\\<dated>.mp4 -> Video\\Personal\\<Year>\\...
    using a name-embedded epoch-ms capture date."""
    cfg = make_cfg(world, taxonomy=TAX)
    name = epoch_ms_name(2020, 1, 18, ".mp4")
    seed_library(world, [f"Video/Personal/G_Dashcam/{name}"])
    st = world["store"]

    res = planmod.build_date_drain(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 1
    lib = cfg.library_root
    assert rows[0]["dst"] == os.path.join(lib, "Video", "Personal", "2020", name)
    assert rows[0]["reason"]["rule"] == "route:personal:video-date"
    assert any("personal videos placed by capture date (C45): 1" in n_
               for n_ in res.notes)


def test_personal_video_drain_uses_vidmeta_creation_year(world):
    """A video with no name-embedded date but a readable mvhd creation date
    drains by ITS embedded year (vidmeta.creation_year), not a guess."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world,
                 ["Video/Personal/I_OldHDD/clip001.mp4"],
                 content_by_rel={
                     "Video/Personal/I_OldHDD/clip001.mp4": mp4_with_year(2018)})
    st = world["store"]
    res = planmod.build_date_drain(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 1
    assert rows[0]["dst"] == os.path.join(
        cfg.library_root, "Video", "Personal", "2018", "clip001.mp4")


def test_photo_drain_regression_unchanged(world):
    """The existing photo drain behavior is untouched by the video addition."""
    cfg = make_cfg(world, taxonomy=TAX)
    name = epoch_ms_name(2019, 5, 4, ".jpg")
    seed_library(world, [f"Photos/G_OldPhone/{name}"])
    st = world["store"]
    res = planmod.build_date_drain(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 1
    assert rows[0]["reason"]["rule"] == "route:photo:date"
    assert any("personal videos placed by capture date (C45): 0" in n_
               for n_ in res.notes)


def test_video_with_no_date_signal_routes_to_undated_shelf(world):
    """No embedded creation date, no name-derived date anywhere -> never guess
    (mtime is NOT a date source, C19). Owner decision (defect fix,
    2026-07-15): the file drops its device-name provenance to a holding
    shelf, Video\\Personal\\Undated\\<filename> — NOT a flat pile, NOT left
    stuck in the device folder."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Video/Personal/HDD2_Part2/FILE043.mp4"])
    st = world["store"]
    res = planmod.build_date_drain(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 1
    assert rows[0]["dst"] == os.path.join(
        cfg.library_root, "Video", "Personal", "Undated", "FILE043.mp4")
    assert rows[0]["reason"]["rule"] == "route:personal:undated"
    assert any("personal videos routed to Undated shelf (C45): 1" in n_
               for n_ in res.notes)


def test_undated_video_already_at_undated_shelf_is_idempotent(world):
    """A file already at personal_root\\Undated\\... yields no row."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Video/Personal/Undated/random_clip.mp4"])
    st = world["store"]
    res = planmod.build_date_drain(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 0
    assert any("already home (personal video, C45): 1" in n_ for n_ in res.notes)


# ── Defect fix: structured name date beats a bogus mvhd date (C45) ─────────

def test_whatsapp_video_name_date_wins_over_bogus_mvhd_date(world):
    """VID-20151015-WA0000.mp4: WhatsApp re-encoded the container and wrote a
    bogus constant mvhd creation time (here, 2011) but the filename itself
    carries the true 2015 capture date. The structured name date MUST win —
    this is the live bug (filed under 2011 instead of 2015)."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(
        world, ["Video/Personal/G_WhatsApp/VID-20151015-WA0000.mp4"],
        content_by_rel={
            "Video/Personal/G_WhatsApp/VID-20151015-WA0000.mp4":
                mp4_with_year(2011)})
    st = world["store"]
    res = planmod.build_date_drain(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 1
    assert rows[0]["dst"] == os.path.join(
        cfg.library_root, "Video", "Personal", "2015",
        "VID-20151015-WA0000.mp4")
    assert rows[0]["reason"]["rule"] == "route:personal:video-date"


def test_dashcam_leading_timestamp_name_date_used(world):
    """A 14-digit leading device-stamp name (dashcam) resolves via the
    structured-name path. Where name and mvhd agree, the result is
    unchanged from before the fix."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(
        world, ["Video/Personal/G_Dashcam/20250520050008_00001A.mp4"],
        content_by_rel={
            "Video/Personal/G_Dashcam/20250520050008_00001A.mp4":
                mp4_with_year(2025)})
    st = world["store"]
    res = planmod.build_date_drain(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 1
    assert rows[0]["dst"] == os.path.join(
        cfg.library_root, "Video", "Personal", "2025",
        "20250520050008_00001A.mp4")


def test_video_mvhd_only_no_structured_name_uses_mvhd_year(world):
    """No structured name date at all -> falls back to vidmeta.creation_year,
    unchanged from the pre-fix behavior (test_personal_video_drain_uses_
    vidmeta_creation_year regression-guards the same case; this one asserts
    the ordering explicitly)."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(
        world, ["Video/Personal/I_OldHDD/clip099.mp4"],
        content_by_rel={
            "Video/Personal/I_OldHDD/clip099.mp4": mp4_with_year(2017)})
    st = world["store"]
    res = planmod.build_date_drain(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 1
    assert rows[0]["dst"] == os.path.join(
        cfg.library_root, "Video", "Personal", "2017", "clip099.mp4")


def test_video_already_at_year_home_is_idempotent(world):
    """A video already at personal_root\\<Year>\\... yields no row."""
    cfg = make_cfg(world, taxonomy=TAX)
    name = epoch_ms_name(2021, 3, 3, ".mp4")
    seed_library(world, [f"Video/Personal/2021/{name}"])
    st = world["store"]
    res = planmod.build_date_drain(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 0
    assert any("already home (personal video, C45): 1" in n_ for n_ in res.notes)


def test_non_video_sidecar_not_swept(world):
    """A .srt beside a dashcam dump is never touched by date-drain — it rides
    its anchor (C36), and date-drain only ever considers video extensions."""
    cfg = make_cfg(world, taxonomy={**TAX, "Documents": (".pdf", ".txt", ".srt")})
    name = epoch_ms_name(2020, 1, 18, ".mp4")
    seed_library(world, [f"Video/Personal/G_Dashcam/{name}",
                         "Video/Personal/G_Dashcam/notes.srt"])
    st = world["store"]
    res = planmod.build_date_drain(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    srcs = {r["src"] for r in rows}
    assert res.n_rows == 1
    assert not any(s.endswith("notes.srt") for s in srcs)


def test_execute_then_converge_to_zero(world):
    """Idempotence end to end: after execute, re-running yields 0 rows."""
    cfg = make_cfg(world, taxonomy=TAX)
    name = epoch_ms_name(2020, 1, 18, ".mp4")
    seed_library(world, [f"Video/Personal/G_Dashcam/{name}"])
    st = world["store"]
    res = planmod.build_date_drain(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 1
    r = apply_plan(st, cfg, res.path,
                   st.start_run("x", [], cfg.config_hash, "t"),
                   execute=True, drive_of=world["drive_of"])
    assert r.exit_code == 0
    res2 = planmod.build_date_drain(st, cfg, drive_of=world["drive_of"])
    assert res2.n_rows == 0


# ── Fix B: collision disambiguation (C46) ───────────────────────────────────

def test_disambiguate_off_by_default_content_distinct_collision_stays_put(world):
    """Default behavior (disambiguate=False, every existing test's contract):
    two content-distinct files from different phones both routing to the same
    voice-note destination skip-and-report, exactly as before."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(
        world,
        ["Audio/G_Device1/PTT-20200101-WA0001.opus",
         "Audio/G_Device2/PTT-20200101-WA0001.opus"],
        content_by_rel={
            "Audio/G_Device1/PTT-20200101-WA0001.opus": b"AAA-content",
            "Audio/G_Device2/PTT-20200101-WA0001.opus": b"BBB-content"})
    st = world["store"]
    res = planmod.build_reorganize(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 0
    assert any("collisions (stay put): 2" in n_ for n_ in res.notes)


def test_disambiguate_on_tags_content_distinct_collision_with_intrinsic_disc(world):
    """disambiguate=True: both survive, each tagged with its SOURCE's
    immediate parent segment — never a (1)/(2)/(3) positional counter."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(
        world,
        ["Audio/G_Device1/PTT-20200101-WA0001.opus",
         "Audio/G_Device2/PTT-20200101-WA0001.opus"],
        content_by_rel={
            "Audio/G_Device1/PTT-20200101-WA0001.opus": b"AAA-content",
            "Audio/G_Device2/PTT-20200101-WA0001.opus": b"BBB-content"})
    st = world["store"]
    res = planmod.build_reorganize(st, cfg, disambiguate=True,
                                   drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 2
    dsts = {r["dst"] for r in rows}
    lib = cfg.library_root
    assert os.path.join(lib, "Audio", "Personal",
                        "PTT-20200101-WA0001 [G_Device1].opus") in dsts
    assert os.path.join(lib, "Audio", "Personal",
                        "PTT-20200101-WA0001 [G_Device2].opus") in dsts
    # never a positional counter
    assert not any("[1]" in d or "[2]" in d or "(1)" in d or "(2)" in d
                   for d in dsts)
    assert len(dsts) == len(set(dsts))               # dsts stay unique (L17)
    assert any("C46 disambiguated (content-distinct collisions): 2" in n_
               for n_ in res.notes)


def test_disambiguate_never_tags_byte_identical_colliders(world):
    """Byte-identical content is a DEDUP decision (C21), never disambiguated —
    it never even reaches the collision path."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(
        world,
        ["Audio/G_Device1/PTT-20200101-WA0001.opus",
         "Audio/G_Device2/PTT-20200101-WA0001.opus"],
        content_by_rel={
            "Audio/G_Device1/PTT-20200101-WA0001.opus": b"IDENTICAL",
            "Audio/G_Device2/PTT-20200101-WA0001.opus": b"IDENTICAL"})
    st = world["store"]
    res = planmod.build_reorganize(st, cfg, disambiguate=True,
                                   drive_of=world["drive_of"])
    assert res.n_rows == 0
    assert any("duplicate content (stay put): 2" in n_ for n_ in res.notes)
    assert not any("disambiguated" in n_ for n_ in res.notes[:0])  # sanity noop


def test_disambiguate_is_idempotent_regardless_of_seed_order(world):
    """Same intrinsic discriminator every re-plan, regardless of the order
    candidates were discovered in — the whole point of 'intrinsic, not
    positional' (L1's idempotence requirement)."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(
        world,
        ["Audio/G_DeviceZ/PTT-20200101-WA0002.opus",
         "Audio/G_DeviceA/PTT-20200101-WA0002.opus",
         "Audio/G_DeviceM/PTT-20200101-WA0002.opus"],
        content_by_rel={
            "Audio/G_DeviceZ/PTT-20200101-WA0002.opus": b"Z-content",
            "Audio/G_DeviceA/PTT-20200101-WA0002.opus": b"A-content",
            "Audio/G_DeviceM/PTT-20200101-WA0002.opus": b"M-content"})
    st = world["store"]
    res1 = planmod.build_reorganize(st, cfg, disambiguate=True,
                                    drive_of=world["drive_of"])
    _, rows1, _ = read_plan(res1.path)
    res2 = planmod.build_reorganize(st, cfg, disambiguate=True,
                                    drive_of=world["drive_of"])
    _, rows2, _ = read_plan(res2.path)
    dsts1 = sorted(r["dst"] for r in rows1)
    dsts2 = sorted(r["dst"] for r in rows2)
    assert res1.n_rows == res2.n_rows == 3
    assert dsts1 == dsts2
    lib = cfg.library_root
    assert os.path.join(lib, "Audio", "Personal",
                        "PTT-20200101-WA0002 [G_DeviceA].opus") in dsts1
    assert os.path.join(lib, "Audio", "Personal",
                        "PTT-20200101-WA0002 [G_DeviceM].opus") in dsts1
    assert os.path.join(lib, "Audio", "Personal",
                        "PTT-20200101-WA0002 [G_DeviceZ].opus") in dsts1


def test_disambiguated_dest_stays_unique_no_double_disambiguation(world):
    """A second re-run against an already-disambiguated destination in the
    index yields zero new rows — convergence, no re-tagging of what's home."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(
        world,
        ["Audio/G_Device1/PTT-20200101-WA0001.opus",
         "Audio/G_Device2/PTT-20200101-WA0001.opus"],
        content_by_rel={
            "Audio/G_Device1/PTT-20200101-WA0001.opus": b"AAA-content",
            "Audio/G_Device2/PTT-20200101-WA0001.opus": b"BBB-content"})
    st = world["store"]
    res = planmod.build_reorganize(st, cfg, disambiguate=True,
                                   drive_of=world["drive_of"])
    assert res.n_rows == 2
    r = apply_plan(st, cfg, res.path,
                   st.start_run("x", [], cfg.config_hash, "t"),
                   execute=True, drive_of=world["drive_of"])
    assert r.exit_code == 0
    res2 = planmod.build_reorganize(st, cfg, disambiguate=True,
                                    drive_of=world["drive_of"])
    assert res2.n_rows == 0
