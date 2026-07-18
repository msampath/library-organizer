"""exif.year_of — hand-built EXIF fixtures, totality by property."""
from __future__ import annotations

import struct

from hypothesis import given, settings, strategies as st

from mlo.exif import year_of

DT = b"2018:09:02 10:11:12\x00"


def le_tiff(dt: bytes = DT) -> bytes:
    t = b"II" + struct.pack("<H", 42) + struct.pack("<I", 8)
    t += struct.pack("<H", 1)
    t += struct.pack("<HHII", 0x8769, 4, 1, 26)      # IFD0: ExifIFD pointer
    t += struct.pack("<I", 0)
    t += struct.pack("<H", 1)
    t += struct.pack("<HHII", 0x9003, 2, len(dt), 44)  # DateTimeOriginal
    t += struct.pack("<I", 0)
    assert len(t) == 44
    return t + dt


def be_tiff(dt: bytes = DT) -> bytes:
    t = b"MM" + struct.pack(">H", 42) + struct.pack(">I", 8)
    t += struct.pack(">H", 1)
    t += struct.pack(">HHII", 0x8769, 4, 1, 26)
    t += struct.pack(">I", 0)
    t += struct.pack(">H", 1)
    t += struct.pack(">HHII", 0x9003, 2, len(dt), 44)
    t += struct.pack(">I", 0)
    return t + dt


def fallback_tiff(dt: bytes = DT) -> bytes:
    """No ExifIFD; IFD0 carries plain DateTime (0x0132)."""
    t = b"II" + struct.pack("<H", 42) + struct.pack("<I", 8)
    t += struct.pack("<H", 1)
    t += struct.pack("<HHII", 0x0132, 2, len(dt), 26)
    t += struct.pack("<I", 0)
    assert len(t) == 26
    return t + dt


def jpeg_wrapping(tiff: bytes) -> bytes:
    payload = b"Exif\x00\x00" + tiff
    return (b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(payload) + 2)
            + payload + b"\xff\xd9")


def write(tmp_path, name: str, data: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_jpeg_little_endian(tmp_path):
    assert year_of(write(tmp_path, "a.jpg", jpeg_wrapping(le_tiff()))) == 2018


def test_jpeg_big_endian(tmp_path):
    assert year_of(write(tmp_path, "b.jpg", jpeg_wrapping(be_tiff()))) == 2018


def test_bare_tiff(tmp_path):
    assert year_of(write(tmp_path, "c.tif", le_tiff())) == 2018


def test_datetime_fallback_in_ifd0(tmp_path):
    assert year_of(write(tmp_path, "d.tif", fallback_tiff())) == 2018


def test_raw_dng_kdc_read_as_tiff(tmp_path):
    """DNG/KDC (and most RAW) are TIFF-based; year_of reads by magic bytes, not
    extension, so RAW files in the Photos bucket get their EXIF year for free.
    Pins the RAW-support the starter template's Photos bucket now relies on."""
    assert year_of(write(tmp_path, "IMG.dng", le_tiff())) == 2018
    assert year_of(write(tmp_path, "IMG.dng", be_tiff())) == 2018
    assert year_of(write(tmp_path, "DCP_0001.kdc", le_tiff())) == 2018
    # a truncated / non-TIFF RAW stays total → None (routes to Photos/Unsorted)
    assert year_of(write(tmp_path, "bad.dng", le_tiff()[:20])) is None
    assert year_of(write(tmp_path, "notraw.dng", b"\x00\x01garbage")) is None


def test_implausible_year_rejected(tmp_path):
    data = jpeg_wrapping(le_tiff(b"1899:01:01 00:00:00\x00"))
    assert year_of(write(tmp_path, "e.jpg", data)) is None


def test_truncated_file(tmp_path):
    data = jpeg_wrapping(le_tiff())[:30]
    assert year_of(write(tmp_path, "f.jpg", data)) is None


def test_png_and_garbage_and_empty(tmp_path):
    assert year_of(write(tmp_path, "g.png", b"\x89PNG\r\n\x1a\n" + b"x" * 50)) is None
    assert year_of(write(tmp_path, "h.jpg", b"\x00\x01\x02garbage")) is None
    assert year_of(write(tmp_path, "i.jpg", b"")) is None
    assert year_of(str(tmp_path / "missing.jpg")) is None


def test_bogus_offsets_do_not_raise(tmp_path):
    """Offsets pointing past the buffer are treated as absent."""
    t = b"II" + struct.pack("<H", 42) + struct.pack("<I", 8)
    t += struct.pack("<H", 1)
    t += struct.pack("<HHII", 0x9003, 2, 20, 999999)   # value offset way out
    t += struct.pack("<I", 0)
    assert year_of(write(tmp_path, "j.tif", t)) is None


@settings(max_examples=25, deadline=None)
@given(st.binary(max_size=4096))
def test_year_of_is_total(tmp_path_factory, data):
    p = tmp_path_factory.mktemp("exif") / "any.jpg"
    p.write_bytes(data)
    out = year_of(str(p))
    assert out is None or isinstance(out, int)
