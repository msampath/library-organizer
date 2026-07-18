"""P21/C3: free-space preflight — pure bytes-needed accounting + a real,
read-only free-space query (no shutil, banned repo-wide)."""
from __future__ import annotations

import os

from mlo import capacity


def _row(kind: str, dst: str, size: int | None) -> dict:
    return {"kind": kind, "dst": dst, "pre": {"size": size}}


def test_bytes_required_counts_copy_in_only(tmp_path):
    d = str(tmp_path)
    rows = [
        _row("copy_in", os.path.join(d, "a.mp4"), 1000),
        _row("copy_in", os.path.join(d, "b.mp4"), 2000),
        _row("move_within", os.path.join(d, "c.mp4"), 5_000_000),
        _row("stage_move", os.path.join(d, "d.mp4"), 5_000_000),
        _row("rmdir_empty", d, None),
    ]
    out = capacity.bytes_required_by_volume(rows)
    assert list(out.values()) == [3000]


def test_bytes_required_groups_by_nearest_existing_ancestor(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    rows = [
        _row("copy_in", str(real / "not-yet" / "created" / "a.mp4"), 1000),
        _row("copy_in", str(real / "not-yet" / "created2" / "b.mp4"), 2000),
    ]
    out = capacity.bytes_required_by_volume(rows)
    assert len(out) == 1
    assert list(out.values())[0] == 3000


def test_bytes_required_aggregates_same_volume_across_distinct_anchors(tmp_path):
    """Super-review M3: two EXISTING destination folders on one volume share
    one free pool — their requirements must sum into a single entry, or a
    combined shortfall passes the preflight unwarned."""
    a = tmp_path / "Video"
    b = tmp_path / "Audio"
    a.mkdir(); b.mkdir()
    rows = [
        _row("copy_in", str(a / "x.mp4"), 1000),
        _row("copy_in", str(b / "y.mp3"), 2000),
    ]
    out = capacity.bytes_required_by_volume(rows)
    assert len(out) == 1
    assert list(out.values())[0] == 3000


def test_bytes_required_splits_by_volume_with_injected_drive_of(tmp_path):
    a = tmp_path / "driveE"
    b = tmp_path / "driveI"
    a.mkdir(); b.mkdir()

    def fake_drive_of(p: str) -> str:
        return "E" if "driveE" in p else "I"

    rows = [
        _row("copy_in", str(a / "x.mp4"), 1000),
        _row("copy_in", str(b / "y.mp3"), 2000),
    ]
    out = capacity.bytes_required_by_volume(rows, drive_of=fake_drive_of)
    assert sorted(out.values()) == [1000, 2000]


def test_free_bytes_returns_a_plausible_number_for_a_real_path(tmp_path):
    free = capacity.free_bytes(str(tmp_path))
    assert free is not None and free > 0


def test_free_bytes_nonexistent_path_never_raises():
    bogus = ("Q:\\definitely\\not\\a\\real\\mount\\path" if os.name == "nt"
             else "/no/such/mount/at/all")
    assert capacity.free_bytes(bogus) is None


def test_preflight_notes_warns_when_short_on_space(tmp_path):
    rows = [_row("copy_in", str(tmp_path / "huge.mp4"), 10 ** 18)]
    notes = capacity.preflight_notes(rows)
    assert len(notes) == 1
    assert "WARNING" in notes[0]


def test_preflight_notes_silent_when_it_fits(tmp_path):
    rows = [_row("copy_in", str(tmp_path / "small.mp4"), 10)]
    assert capacity.preflight_notes(rows) == []


def test_preflight_notes_empty_for_no_copy_in_rows(tmp_path):
    rows = [_row("move_within", str(tmp_path / "x.mp4"), 5_000_000)]
    assert capacity.preflight_notes(rows) == []
