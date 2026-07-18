"""ole.read — legacy CFB SummaryInformation reader. The full compound-file walk
is validated against real .ppt/.xls in the library; here we pin the risky
property-set decoding and the total-by-contract graceful paths."""
from __future__ import annotations

import struct

from mlo import ole


def _property_set(props: dict[int, tuple[int, bytes]]) -> bytes:
    """Build a one-section OLEPS property set. props: pid -> (VT, value bytes)."""
    n = len(props)
    table = b""
    values = b""
    table_len = 8 + n * 8                       # size(4)+nprops(4)+entries
    for pid, (vt, vb) in props.items():
        val = struct.pack("<I", vt) + vb
        table += struct.pack("<II", pid, table_len + len(values))
        values += val
    section = struct.pack("<II", table_len + len(values), n) + table + values
    header = (struct.pack("<HHI", 0xFFFE, 0, 0) + b"\x00" * 16
              + struct.pack("<I", 1) + b"\x00" * 16 + struct.pack("<I", 48))
    return header + section


def test_lpstr_title_and_creator():
    def lpstr(s):
        b = s.encode("cp1252") + b"\x00"
        return (0x1E, struct.pack("<I", len(b)) + b)
    blob = _property_set({2: lpstr("Sci-Tech Quiz Show"),
                          4: lpstr("Example Author")})
    out = ole._property_set(blob, {2: "title", 4: "creator"})
    assert out == {"title": "Sci-Tech Quiz Show",
                   "creator": "Example Author"}


def test_lpwstr_unicode_value():
    s = "Präsentación"
    wb = s.encode("utf-16-le") + b"\x00\x00"
    blob = _property_set({2: (0x1F, struct.pack("<I", len(s) + 1) + wb)})
    assert ole._property_set(blob, {2: "title"})["title"] == s


def test_filetime_becomes_date():
    # 2013-10-04 as Windows FILETIME (100ns ticks since 1601)
    import datetime
    ticks = int((datetime.datetime(2013, 10, 4)
                 - datetime.datetime(1601, 1, 1)).total_seconds()) * 10_000_000
    blob = _property_set({12: (0x40, struct.pack("<II", ticks & 0xFFFFFFFF,
                                                 ticks >> 32))})
    assert ole._property_set(blob, {12: "created"})["created"] == "2013-10-04"


def test_non_ole_and_corrupt_return_empty(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_bytes(b"plain text, not a compound file")
    assert ole.read(str(p)) == {}
    # right signature, garbage after -> caught, {} (never raises)
    bad = tmp_path / "bad.xls"
    bad.write_bytes(ole._SIG + b"\x00" * 600)
    assert ole.read(str(bad)) == {}
    assert ole.read(str(tmp_path / "missing.ppt")) == {}
