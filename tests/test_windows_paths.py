"""The Windows reality matrix (defect L10): >260-char paths, lone-surrogate
filenames, reserved device names. Run for real on Windows; skipped elsewhere
(POSIX CI keeps the portable code honest, Windows CI keeps this file honest)."""
from __future__ import annotations

import os
import sys

import pytest

from conftest import make_file
from mlo import fingerprint, winpath

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")


@windows_only
def test_long_path_end_to_end(world, kernel):
    deep = world["E"]
    for i in range(12):
        deep = deep / ("segment-" + "x" * 20 + f"-{i:02d}")
    src = deep / ("file-" + "y" * 40 + ".bin")
    assert len(str(src)) > 260
    os.makedirs(winpath.to_long(str(deep)), exist_ok=True)
    with open(winpath.to_long(str(src)), "wb") as f:
        f.write(b"deep content")

    size, qh = fingerprint.quick(str(src))
    rel = os.path.relpath(str(src), str(world["E"]))
    dst = world["E"] / "Delete" / rel
    res = kernel(execute=True).stage_move(str(src), str(dst), size, qh)
    assert res.status == "done"
    assert os.path.exists(winpath.to_long(str(dst)))
    assert not os.path.exists(winpath.to_long(str(src)))


@windows_only
def test_lone_surrogate_filename_end_to_end(world, kernel):
    name = "clip-\udcc3\udca9.mp4"          # NTFS permits this; UTF-8 does not
    src = world["E"] / "weird"
    src.mkdir(parents=True, exist_ok=True)
    lsrc = winpath.to_long(str(src / name))
    with open(lsrc, "wb") as f:
        f.write(b"surrogate content")

    size, qh = fingerprint.quick(str(src / name))
    dst = world["E"] / "Delete" / "weird" / name
    res = kernel(execute=True).stage_move(str(src / name), str(dst), size, qh)
    assert res.status == "done"

    # the journal stored it losslessly (BLOB) and displays it lossily
    ops = list(world["store"].export_ops())
    assert any("clip-" in op["src_display"] for op in ops)


@windows_only
def test_reserved_device_names_do_not_hang(world):
    # CON/NUL as *names inside* a directory: stat must not block or crash the
    # walker; with \\?\ semantics they are ordinary names.
    d = world["E"] / "devices"
    d.mkdir(exist_ok=True)
    created = []
    for name in ("CON", "NUL.txt", "aux.mp3"):
        lp = winpath.to_long(str(d / name))
        try:
            with open(lp, "wb") as f:
                f.write(b"x")
            created.append(name)
        except OSError:
            pass                              # some names may still be refused
    for name in created:
        size, qh = fingerprint.quick(str(d / name))
        assert size == 1


def test_case_insensitive_duplicate_dst_detection_matches_platform():
    from mlo.plan import PlanError, _rows_unique_dsts
    rows = [{"src": "a", "dst": "X\\same.bin"},
            {"src": "b", "dst": "x\\SAME.BIN"}]
    if os.name == "nt":
        with pytest.raises(PlanError):
            _rows_unique_dsts(rows)
    else:
        _rows_unique_dsts(rows)               # distinct paths on POSIX
