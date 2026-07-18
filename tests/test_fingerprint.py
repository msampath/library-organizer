from __future__ import annotations

import hashlib

from conftest import make_file
from mlo import fingerprint


def test_small_file_hashes_whole_content(tmp_path):
    f = make_file(tmp_path / "small.bin", b"tiny")
    size, qh = fingerprint.quick(str(f))
    assert size == 4
    assert qh == hashlib.sha256(b"tiny").hexdigest()


def test_large_file_hashes_head_and_tail(tmp_path):
    head = b"H" * fingerprint.CHUNK
    middle = b"M" * 1000
    tail_region = b"T" * fingerprint.CHUNK
    f = make_file(tmp_path / "big.bin", head + middle + tail_region)
    size, qh = fingerprint.quick(str(f))
    assert size == len(head) + 1000 + len(tail_region)
    expect = hashlib.sha256(head + tail_region).hexdigest()
    assert qh == expect


def test_middle_change_is_invisible_to_quick_but_not_full(tmp_path):
    base = b"H" * fingerprint.CHUNK + b"MIDDLE" + b"T" * fingerprint.CHUNK
    changed = b"H" * fingerprint.CHUNK + b"M1DDLE" + b"T" * fingerprint.CHUNK
    a = make_file(tmp_path / "a.bin", base)
    b = make_file(tmp_path / "b.bin", changed)
    assert fingerprint.quick(str(a)) == fingerprint.quick(str(b))  # known limit
    assert fingerprint.full(str(a)) != fingerprint.full(str(b))    # escalation


def test_full_hash(tmp_path):
    f = make_file(tmp_path / "c.bin", b"abc" * 100)
    assert fingerprint.full(str(f)) == hashlib.sha256(b"abc" * 100).hexdigest()


def test_region_bounded_reads_are_byte_identical_to_a_single_big_read(
        tmp_path, monkeypatch):
    """P21/A1: region() must hash the exact same bytes in the exact same order
    whether it reads in one big gulp or many small sub-chunks — the fix is a
    memory-bound refactor, not a behavior change. Force many loop iterations on
    a small file by shrinking _SUBCHUNK, and compare against hashlib computed
    directly over the same head+tail byte ranges."""
    monkeypatch.setattr(fingerprint, "_SUBCHUNK", 7)   # forces many iterations
    head = b"H" * 50
    middle = b"M" * 30
    tail_region = b"T" * 50
    data = head + middle + tail_region
    f = make_file(tmp_path / "multi.bin", data)

    chunk = 50
    size, qh = fingerprint.region(str(f), chunk=chunk)
    assert size == len(data)
    expect = hashlib.sha256(data[:chunk] + data[-chunk:]).hexdigest()
    assert qh == expect

    # chunk larger than the file: head-only, exactly like the un-chunked path.
    size2, qh2 = fingerprint.region(str(f), chunk=10_000)
    assert qh2 == hashlib.sha256(data).hexdigest()


def test_confirm_duplicate_small_file_trusts_quick_hash(tmp_path):
    """P21/A3: at or below the 256 KiB threshold, quick()'s head+tail regions
    already cover the whole file, so a quick match alone confirms."""
    a = make_file(tmp_path / "a.txt", b"same content")
    b = make_file(tmp_path / "b.txt", b"same content")
    assert fingerprint.confirm_duplicate(str(a), str(b)) is True


def test_confirm_duplicate_large_file_escalates_to_full_hash(tmp_path):
    """Above the threshold, a same-size/same-ends/different-middle pair passes
    the quick screen but must be REFUSED once confirm_duplicate escalates —
    the exact false-positive class this function exists to close."""
    ch = 128 * 1024
    base = b"H" * ch + b"MIDDLE" * 60_000 + b"T" * ch     # > 256 KiB
    changed = b"H" * ch + b"M1DDLE" * 60_000 + b"T" * ch
    a = make_file(tmp_path / "big_a.bin", base)
    b = make_file(tmp_path / "big_b.bin", changed)
    c = make_file(tmp_path / "big_c.bin", base)           # true twin of a
    assert len(base) > 256 * 1024
    assert fingerprint.quick(str(a)) == fingerprint.quick(str(b))   # quick blind spot
    assert fingerprint.confirm_duplicate(str(a), str(b)) is False   # A3 catches it
    assert fingerprint.confirm_duplicate(str(a), str(c)) is True    # genuine twin


def test_confirm_duplicate_exactly_at_threshold_uses_quick_only(tmp_path):
    data = b"X" * (256 * 1024)
    a = make_file(tmp_path / "at1.bin", data)
    b = make_file(tmp_path / "at2.bin", data)
    assert fingerprint.confirm_duplicate(str(a), str(b),
                                         full_threshold=256 * 1024) is True


def test_confirm_duplicate_size_mismatch_refutes_without_full_read(tmp_path):
    a = make_file(tmp_path / "s1.bin", b"x" * 10)
    b = make_file(tmp_path / "s2.bin", b"x" * 20)
    assert fingerprint.confirm_duplicate(str(a), str(b)) is False


def test_confirm_duplicate_unreadable_side_is_false_never_raises(tmp_path):
    a = make_file(tmp_path / "exists.bin", b"data")
    gone = tmp_path / "gone.bin"
    assert fingerprint.confirm_duplicate(str(a), str(gone)) is False
    assert fingerprint.confirm_duplicate(str(gone), str(a)) is False


def test_confirm_same_tiers_and_escalation(tmp_path):
    """The redundant-move confirmation a critic must pass: identical content
    confirms; a same-size / same-ends / different-middle pair confirms at the
    quick tier (the known blind spot) but is REFUSED once the caller escalates
    the chunk past the head/tail regions — the sanctioned destructive bar."""
    base = b"H" * fingerprint.CHUNK + b"MIDDLE" + b"T" * fingerprint.CHUNK
    changed = b"H" * fingerprint.CHUNK + b"M1DDLE" + b"T" * fingerprint.CHUNK
    a = make_file(tmp_path / "a.bin", base)
    b = make_file(tmp_path / "b.bin", changed)
    c = make_file(tmp_path / "c.bin", base)               # true twin of a
    assert fingerprint.confirm_same(str(a), str(c)) is True
    assert fingerprint.confirm_same(str(a), str(b)) is True          # quick blind spot
    big = fingerprint.CHUNK + 6                            # chunk spans the middle
    assert fingerprint.confirm_same(str(a), str(b), chunk=big) is False
    # total: an unreadable side is "not confirmed", never an exception
    assert fingerprint.confirm_same(str(a), str(tmp_path / "gone.bin")) is False
