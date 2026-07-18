"""Kernel behavior — every test here guards a defect-ledger entry."""
from __future__ import annotations

import os

import pytest

from conftest import make_file
from mlo import fingerprint
from mlo.safeops import op_id_for


def _pre(path) -> tuple[int, str]:
    return fingerprint.quick(str(path))


# ── policy: both ends (L12) ─────────────────────────────────────────────────

def test_protected_source_blocked(world, kernel):
    src = make_file(world["E"] / "BlueStacks_data" / "disk.img")
    dst = world["E"] / "Delete" / "BlueStacks_data" / "disk.img"
    res = kernel(execute=True).stage_move(str(src), str(dst), *_pre(src))
    assert res.status == "skipped_protected"
    assert src.exists()


def test_protected_destination_blocked(world, kernel):
    src = make_file(world["E"] / "stuff" / "a.txt")
    dst = world["E"] / "Delete" / "bluestacks-ish" / "a.txt"
    res = kernel(execute=True).stage_move(str(src), str(dst), *_pre(src))
    assert res.status == "skipped_protected"
    assert "dst" in res.detail
    assert src.exists()


def test_blocked_drive_policy(world):
    policy = world["policy"]
    probe = str(world["tmp"] / "outside" / "f.txt")   # fake drive_of says 'Z'
    assert policy.check(probe) is None

    def c_drive_of(path: str) -> str:
        return "C"

    from mlo.safeops import PathPolicy
    blocked_policy = PathPolicy(
        protected_substrings=(), blocked_drives=("C", "F"),
        staging_roots={}, library_root=str(world["lib"]),
        drive_of=c_drive_of)
    hit = blocked_policy.check(probe)
    assert hit is not None and "protected drive C" in hit.reason


# ── placement rules are programming errors, not runtime skips ───────────────

def test_stage_move_cross_drive_is_plan_bug(world, kernel):
    src = make_file(world["E"] / "x" / "f.bin")
    dst = world["I"] / "Delete" / "f.bin"
    with pytest.raises(ValueError, match="same-drive"):
        kernel(execute=True).stage_move(str(src), str(dst), *_pre(src))


def test_stage_move_outside_staging_root_is_plan_bug(world, kernel):
    src = make_file(world["E"] / "x" / "g.bin")
    dst = world["E"] / "elsewhere" / "g.bin"
    with pytest.raises(ValueError, match="staging root"):
        kernel(execute=True).stage_move(str(src), str(dst), *_pre(src))


def test_copy_in_must_target_library(world, kernel):
    src = make_file(world["E"] / "x" / "h.bin")
    dst = world["I"] / "not-library" / "h.bin"
    with pytest.raises(ValueError, match="library root"):
        kernel(execute=True).copy_in(str(src), str(dst), *_pre(src))


# ── drift is data (L9, L17) ─────────────────────────────────────────────────

def test_missing_source_is_drift_in_dry_run_too(world, kernel):
    src = world["E"] / "gone" / "missing.bin"      # never created
    dst = world["E"] / "Delete" / "gone" / "missing.bin"
    res = kernel(execute=False).stage_move(str(src), str(dst), None, None)
    assert res.status == "skipped_drift"
    assert "source missing" in res.detail


def test_occupied_destination_is_drift_never_a_new_name(world, kernel):
    src = make_file(world["E"] / "d" / "same.bin", b"AAA")
    dst = make_file(world["E"] / "Delete" / "d" / "same.bin", b"BBB")
    res = kernel(execute=True).stage_move(str(src), str(dst), *_pre(src))
    assert res.status == "skipped_drift"
    assert "occupied" in res.detail
    assert src.read_bytes() == b"AAA" and dst.read_bytes() == b"BBB"


def test_content_drift_detected(world, kernel):
    src = make_file(world["E"] / "d" / "drifty.bin", b"planned content")
    size, qh = _pre(src)
    src.write_bytes(b"changed after planning!")
    dst = world["E"] / "Delete" / "d" / "drifty.bin"
    res = kernel(execute=True).stage_move(str(src), str(dst), size, qh)
    assert res.status == "skipped_drift"
    assert src.exists() and not dst.exists()


# ── dry-run and execute share one path (L9) ─────────────────────────────────

def test_dry_run_touches_nothing_and_journals_nothing(world, kernel):
    src = make_file(world["E"] / "dr" / "f.bin")
    dst = world["E"] / "Delete" / "dr" / "f.bin"
    res = kernel(execute=False).stage_move(str(src), str(dst), *_pre(src))
    assert res.status == "would_do"
    assert src.exists() and not dst.exists()
    assert world["store"].journal_pos() == 0


# ── idempotency (L1) ────────────────────────────────────────────────────────

def test_double_execute_is_noop(world, kernel):
    src = make_file(world["E"] / "i" / "f.bin", b"payload")
    dst = world["E"] / "Delete" / "i" / "f.bin"
    pre = _pre(src)
    k = kernel(execute=True)
    first = k.stage_move(str(src), str(dst), *pre)
    assert first.status == "done"
    second = k.stage_move(str(src), str(dst), *pre)
    assert second.status == "skipped_done"
    assert dst.read_bytes() == b"payload" and not src.exists()


def test_op_id_is_content_addressed():
    a = op_id_for("copy_in", "E:\\a", "I:\\lib\\a", 10, "h1")
    b = op_id_for("copy_in", "E:\\a", "I:\\lib\\a", 10, "h1")
    c = op_id_for("copy_in", "E:\\a", "I:\\lib\\a", 11, "h1")
    assert a == b != c


# ── copy_in verifies its own copy (L15) ─────────────────────────────────────

def test_copy_in_happy_path_updates_index(world, kernel):
    src = make_file(world["E"] / "c" / "keep.jpg", b"J" * 5000)
    dst = world["lib"] / "Photos" / "keep.jpg"
    size, qh = _pre(src)
    res = kernel(execute=True).copy_in(str(src), str(dst), size, qh)
    assert res.status == "done"
    assert dst.read_bytes() == b"J" * 5000
    assert src.exists()                          # copy, not move
    rel = os.path.relpath(str(dst), str(world["lib"]))
    assert world["store"].index_get(rel) == {
        "size": size, "quick_hash": qh,
        "mtime_ns": world["store"].index_get(rel)["mtime_ns"]}


def test_copy_in_preserves_source_mtime(world, kernel):
    """P21/A2: consolidating a 10-year-old photo must not stamp it 'today' —
    the whole point of a media library organizer is preserving capture dates."""
    src = make_file(world["E"] / "c" / "old.jpg", b"J" * 100)
    old_ns = 1_262_304_000 * 10**9   # 2010-01-01T00:00:00Z, well before "now"
    os.utime(str(src), ns=(old_ns, old_ns))
    dst = world["lib"] / "Photos" / "old.jpg"
    size, qh = _pre(src)
    res = kernel(execute=True).copy_in(str(src), str(dst), size, qh)
    assert res.status == "done"
    assert os.stat(str(dst)).st_mtime_ns == old_ns
    rel = os.path.relpath(str(dst), str(world["lib"]))
    assert world["store"].index_get(rel)["mtime_ns"] == old_ns


def test_copy_in_detects_corrupt_copy(world, kernel, monkeypatch):
    src = make_file(world["E"] / "c" / "fragile.bin", b"REAL CONTENT")
    dst = world["lib"] / "Backups" / "fragile.bin"
    size, qh = _pre(src)

    from mlo.safeops import SafeOps

    def corrupt_copy(lsrc, lpart):
        with open(lpart, "xb") as f:
            f.write(b"CORRUPTED!!")

    monkeypatch.setattr(SafeOps, "_copy_stream", staticmethod(corrupt_copy))
    res = kernel(execute=True).copy_in(str(src), str(dst), size, qh)
    assert res.status == "failed"
    assert "mismatch" in res.detail
    assert not dst.exists()                      # bad copy never took the name
    assert dst.with_name(dst.name + ".mlopart").exists()  # inert, reportable
    rel = os.path.relpath(str(dst), str(world["lib"]))
    assert world["store"].index_get(rel) is None  # never entered the index


# ── index effects are transactional with ops (L7) ───────────────────────────

def test_move_within_moves_index_row(world, kernel):
    inside = make_file(world["lib"] / "Other" / "misfiled.mp3", b"M" * 300)
    size, qh = _pre(inside)
    world["store"].index_upsert("Other\\misfiled.mp3" if os.name == "nt"
                                else "Other/misfiled.mp3",
                                size, qh, 0, "scan-x")
    world["store"].index_commit()
    dst = world["lib"] / "Audio" / "misfiled.mp3"
    res = kernel(execute=True).move_within(str(inside), str(dst), size, qh)
    assert res.status == "done"
    old_rel = os.path.join("Other", "misfiled.mp3")
    new_rel = os.path.join("Audio", "misfiled.mp3")
    assert world["store"].index_get(old_rel) is None
    assert world["store"].index_get(new_rel) is not None


def test_stage_move_out_of_library_deletes_index_row(world, kernel):
    inside = make_file(world["lib"] / "Junk" / "dupe.bin", b"D" * 64)
    size, qh = _pre(inside)
    rel = os.path.join("Junk", "dupe.bin")
    world["store"].index_upsert(rel, size, qh, 0, "scan-x")
    world["store"].index_commit()
    dst = world["I"] / "Delete" / "Junk" / "dupe.bin"
    res = kernel(execute=True).stage_move(str(inside), str(dst), size, qh)
    assert res.status == "done"
    assert world["store"].index_get(rel) is None


# ── rmdir_empty (L18) ───────────────────────────────────────────────────────

def test_rmdir_empty_removes_only_empty(world, kernel):
    d = world["E"] / "emptyish"
    d.mkdir()
    res = kernel(execute=True).rmdir_empty(str(d))
    assert res.status == "done" and not d.exists()


def test_rmdir_empty_fails_on_content_and_keeps_it(world, kernel):
    d = world["E"] / "full"
    f = make_file(d / "precious.txt", b"do not lose me")
    res = kernel(execute=True).rmdir_empty(str(d))
    assert res.status == "failed"
    assert f.read_bytes() == b"do not lose me"


# ── drift/protected outcomes are journaled in execute mode (L16) ────────────

def test_execute_journals_drift_with_resolved_state(world, kernel):
    src = world["E"] / "nope" / "ghost.bin"
    dst = world["E"] / "Delete" / "nope" / "ghost.bin"
    res = kernel(execute=True).stage_move(str(src), str(dst), None, None)
    assert res.status == "skipped_drift"
    assert world["store"].op_state(res.op_id) == "skipped_drift"


# ── dispose (P21/C2 — the L18 amendment) ─────────────────────────────────────
# The real OS call is injected (`disposer=`) — these tests verify the
# KERNEL's placement/drift/journal/idempotence logic, which is where an
# actual defect would live; the real Windows/POSIX call is exercised
# separately (tests/test_trash.py's pure helpers + owner-verified live use).

def _fake_disposer(calls: list):
    def disposer(lpath: str, display_path: str) -> None:
        calls.append((lpath, display_path))
    return disposer


def test_dispose_under_staging_root_succeeds_and_calls_the_disposer(world, kernel):
    f = make_file(world["E"] / "Delete" / "d" / "junk.bin")
    calls: list = []
    res = kernel(execute=True, disposer=_fake_disposer(calls)).dispose(str(f), *_pre(f))
    assert res.status == "done"
    assert len(calls) == 1
    assert calls[0][1] == str(f)


def test_dispose_outside_any_staging_root_is_plan_bug(world, kernel):
    f = make_file(world["E"] / "not-staging" / "junk.bin")
    with pytest.raises(ValueError, match="staging root"):
        kernel(execute=True, disposer=_fake_disposer([])).dispose(str(f), *_pre(f))


def test_dispose_missing_source_is_drift_never_calls_disposer(world, kernel):
    f = world["E"] / "Delete" / "gone.bin"     # never created
    calls: list = []
    res = kernel(execute=True, disposer=_fake_disposer(calls)).dispose(str(f), None, None)
    assert res.status == "skipped_drift"
    assert calls == []


def test_dispose_protected_source_is_blocked_never_calls_disposer(world, kernel):
    f = make_file(world["E"] / "Delete" / "BlueStacks_data" / "disk.img")
    calls: list = []
    res = kernel(execute=True, disposer=_fake_disposer(calls)).dispose(str(f), *_pre(f))
    assert res.status == "skipped_protected"
    assert calls == []


def test_dispose_rehearse_does_not_call_the_disposer(world, kernel):
    f = make_file(world["E"] / "Delete" / "d" / "junk.bin")
    calls: list = []
    res = kernel(execute=False, disposer=_fake_disposer(calls)).dispose(str(f), *_pre(f))
    assert res.status == "would_do"
    assert calls == []
    assert f.exists()   # rehearsal never touches disk


def test_dispose_is_idempotent_second_call_is_journal_gated(world, kernel):
    f = make_file(world["E"] / "Delete" / "d" / "junk.bin")
    calls: list = []
    k = kernel(execute=True, disposer=_fake_disposer(calls))
    pre = _pre(f)
    first = k.dispose(str(f), *pre)
    assert first.status == "done"
    second = k.dispose(str(f), *pre)
    assert second.status == "skipped_done"
    assert len(calls) == 1     # the disposer never runs twice for one op_id


def test_dispose_on_a_directory_is_drift_never_reaches_the_disposer(world, kernel):
    """Kernel API robustness (super-review): a directory handed to dispose
    would give the OS recycle/trash call a whole tree — refused as drift."""
    d = world["E"] / "Delete" / "subdir"
    make_file(d / "inner.bin")
    calls: list = []
    res = kernel(execute=True, disposer=_fake_disposer(calls)).dispose(
        str(d), None, None)
    assert res.status == "skipped_drift"
    assert "not a regular file" in res.detail
    assert calls == []


def test_default_disposer_windows_path_is_never_long_prefixed(world, monkeypatch):
    """Super-review H1 (empirically reproduced): SHFileOperationW rejects
    \\\\?\\-prefixed paths with 0x7C and recycles nothing. The dispatcher must
    hand the Windows call the PLAIN path."""
    from mlo import safeops as so
    from mlo import winpath
    seen: list = []
    monkeypatch.setattr(so, "_recycle_windows", lambda p: seen.append(p))
    monkeypatch.setattr(so, "_trash_posix",
                        lambda lp, dp: seen.append(dp))
    monkeypatch.setattr(winpath, "is_windows", lambda: True)
    f = make_file(world["E"] / "Delete" / "h1.bin")
    so._default_disposer(winpath.to_long(str(f)), str(f))
    assert seen == [str(f)]
    assert not seen[0].startswith("\\\\?\\")


def test_trash_posix_claims_info_first_and_percent_encodes(world, monkeypatch, tmp_path):
    """The POSIX trash path end-to-end on tmp dirs (pure Python — runs on any
    OS): the .trashinfo exclusive-create claims the name before the rename,
    a stale info/ entry pushes the name on, and Path= is percent-encoded."""
    from mlo import safeops as so
    from mlo import trash
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    files_dir, info_dir = trash.home_trash_dirs(str(xdg))
    os.makedirs(info_dir, exist_ok=True)
    # a stale claim on the natural name forces the (1) suffix
    with open(os.path.join(info_dir, "junk.bin.trashinfo"), "w") as fh:
        fh.write("stale")

    f = make_file(tmp_path / "staging" / "junk.bin", b"payload")
    so._trash_posix(str(f), str(f))

    assert not f.exists()
    assert os.path.exists(os.path.join(files_dir, "junk (1).bin"))
    info_text = open(os.path.join(info_dir, "junk (1).bin.trashinfo")).read()
    assert info_text.startswith("[Trash Info]\nPath=")
    assert "junk.bin" in info_text or "junk%2Ebin" in info_text
