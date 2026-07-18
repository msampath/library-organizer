"""vidmeta.creation_year — hand-built MP4 atom fixtures, totality by property."""
from __future__ import annotations

import struct

from hypothesis import given, settings, strategies as st

from mlo.vidmeta import creation_year

_MAC_EPOCH_DELTA = 2082844800


def _atom(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def _mvhd_v0(creation: int) -> bytes:
    # version(1) + flags(3) + creation_time(4) + modification_time(4) +
    # timescale(4) + duration(4) + rest (irrelevant, omitted)
    payload = bytes([0, 0, 0, 0]) + struct.pack(">I", creation) \
        + struct.pack(">I", 0) + struct.pack(">I", 600) + struct.pack(">I", 0)
    return _atom(b"mvhd", payload)


def _mvhd_v1(creation: int) -> bytes:
    payload = bytes([1, 0, 0, 0]) + struct.pack(">Q", creation) \
        + struct.pack(">Q", 0) + struct.pack(">I", 600) + struct.pack(">Q", 0)
    return _atom(b"mvhd", payload)


def _mp4(mvhd: bytes) -> bytes:
    ftyp = _atom(b"ftyp", b"isom" + struct.pack(">I", 0) + b"isomiso2avc1mp41")
    moov = _atom(b"moov", mvhd)
    return ftyp + moov


def write(tmp_path, name: str, data: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_v0_creation_year(tmp_path):
    ts = int(
        __import__("datetime").datetime(2020, 1, 18, tzinfo=__import__("datetime").timezone.utc)
        .timestamp()) + _MAC_EPOCH_DELTA
    data = _mp4(_mvhd_v0(ts))
    assert creation_year(write(tmp_path, "a.mp4", data)) == 2020


def test_v1_creation_year(tmp_path):
    import datetime
    ts = int(datetime.datetime(2019, 6, 1, tzinfo=datetime.timezone.utc)
             .timestamp()) + _MAC_EPOCH_DELTA
    data = _mp4(_mvhd_v1(ts))
    assert creation_year(write(tmp_path, "b.mov", data)) == 2019


def test_zero_epoch_rejected(tmp_path):
    """creation_time == 0 decodes to 1904 — the 'unset' sentinel, not a date."""
    data = _mp4(_mvhd_v0(0))
    assert creation_year(write(tmp_path, "c.mp4", data)) is None


def test_implausible_year_rejected(tmp_path):
    import datetime
    ts = int(datetime.datetime(2040, 1, 1, tzinfo=datetime.timezone.utc)
             .timestamp()) + _MAC_EPOCH_DELTA
    data = _mp4(_mvhd_v0(ts))
    assert creation_year(write(tmp_path, "d.mp4", data)) is None


def test_no_moov(tmp_path):
    data = _atom(b"ftyp", b"isom" + b"\x00" * 12)
    assert creation_year(write(tmp_path, "e.mp4", data)) is None


def test_no_mvhd_inside_moov(tmp_path):
    data = _atom(b"ftyp", b"isom") + _atom(b"moov", _atom(b"trak", b"x" * 8))
    assert creation_year(write(tmp_path, "f.mp4", data)) is None


def test_truncated_and_garbage_and_empty(tmp_path):
    good = _mp4(_mvhd_v0(int(_MAC_EPOCH_DELTA + 1))
                if False else _mvhd_v0(_MAC_EPOCH_DELTA + 1577836800))
    assert creation_year(write(tmp_path, "g.mp4", good[:20])) is None
    assert creation_year(write(tmp_path, "h.mp4", b"\x00\x01\x02garbage")) is None
    assert creation_year(write(tmp_path, "i.mp4", b"")) is None
    assert creation_year(str(tmp_path / "missing.mp4")) is None


def test_bogus_size_does_not_raise(tmp_path):
    """A declared atom size larger than the buffer must not raise or hang."""
    bogus = struct.pack(">I", 999999) + b"moov" + b"x" * 20
    assert creation_year(write(tmp_path, "j.mp4", bogus)) is None


def test_extended_size_atom(tmp_path):
    """size==1 -> 64-bit extended size follows the type."""
    mvhd = _mvhd_v0(_MAC_EPOCH_DELTA + 1577836800)   # 2020-01-01
    ext_moov = struct.pack(">I", 1) + b"moov" + struct.pack(">Q", 16 + len(mvhd)) + mvhd
    ftyp = _atom(b"ftyp", b"isom")
    assert creation_year(write(tmp_path, "k.mp4", ftyp + ext_moov)) == 2020


@settings(max_examples=50, deadline=None)
@given(st.binary(max_size=4096))
def test_creation_year_is_total(tmp_path_factory, data):
    p = tmp_path_factory.mktemp("vidmeta") / "any.mp4"
    p.write_bytes(data)
    out = creation_year(str(p))
    assert out is None or isinstance(out, int)
