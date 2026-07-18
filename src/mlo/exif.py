"""Minimal EXIF date reader — stdlib only, read-only, TOTAL.

year_of() returns the photo's DateTimeOriginal year (falling back to IFD0
DateTime) for JPEG and bare TIFF files, or None. It NEVER raises: a photo
without derivable EXIF routes to Photos/Unsorted rather than being guessed
from mtime, which lies after every copy (see taxonomy.route()).

Hard limits, all deliberate:
  - only the first 1 MiB is read; any offset past the cap is treated as absent;
  - IFDs are capped at 512 entries and one IFD0 -> ExifIFD hop (no chains);
  - years outside 1900-2035 are rejected (same plausibility window as
    naming.parse_year — L3's lesson applies to metadata too);
  - PNG/HEIC return None (HEIC needs a dependency; roadmap).
"""
from __future__ import annotations

import struct

from . import winpath

_CAP = 1024 * 1024
_MAX_ENTRIES = 512
_TAG_EXIF_IFD = 0x8769
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_DATETIME = 0x0132


def year_of(path: str) -> int | None:
    try:
        with open(winpath.to_long(path), "rb") as f:
            data = f.read(_CAP)
    except OSError:
        return None
    try:
        return _year_from_bytes(data)
    except Exception:          # total by contract — malformed metadata is "no year"
        return None


def _year_from_bytes(data: bytes) -> int | None:
    if data[:2] == b"\xff\xd8":
        tiff = _exif_tiff_from_jpeg(data)
    elif data[:4] in (b"II*\x00", b"MM\x00*"):
        tiff = data
    else:
        return None
    if tiff is None:
        return None
    return _tiff_datetime_year(tiff)


def _exif_tiff_from_jpeg(data: bytes) -> bytes | None:
    """Walk JPEG segments to APP1 'Exif\\x00\\x00'; stop at SOS or bad structure."""
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:               # fill byte
            i += 1
            continue
        if marker in (0x01,) or 0xD0 <= marker <= 0xD8:   # no payload
            i += 2
            continue
        if marker == 0xDA:               # start of scan — EXIF must precede
            return None
        seglen = int.from_bytes(data[i + 2:i + 4], "big")
        if seglen < 2:
            return None
        if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\x00\x00":
            return data[i + 10:i + 2 + seglen]
        i += 2 + seglen
    return None


def _tiff_datetime_year(t: bytes) -> int | None:
    if len(t) < 8:
        return None
    if t[:2] == b"II":
        endian = "<"
    elif t[:2] == b"MM":
        endian = ">"
    else:
        return None
    if struct.unpack(endian + "H", t[2:4])[0] != 42:
        return None
    ifd0 = struct.unpack(endian + "I", t[4:8])[0]

    exif_ptr: int | None = None
    fallback: bytes | None = None
    for tag, typ, count, valfield in _ifd_entries(t, endian, ifd0):
        if tag == _TAG_EXIF_IFD and typ == 4:
            exif_ptr = struct.unpack(endian + "I", valfield)[0]
        elif tag == _TAG_DATETIME and typ == 2:
            fallback = _ascii_value(t, endian, count, valfield)

    if exif_ptr is not None:             # one hop only, by construction
        for tag, typ, count, valfield in _ifd_entries(t, endian, exif_ptr):
            if tag == _TAG_DATETIME_ORIGINAL and typ == 2:
                year = _parse_year(_ascii_value(t, endian, count, valfield))
                if year is not None:
                    return year
    return _parse_year(fallback)


def _ifd_entries(t: bytes, endian: str, offset: int):
    if offset < 0 or offset + 2 > len(t):
        return
    n = struct.unpack(endian + "H", t[offset:offset + 2])[0]
    if n > _MAX_ENTRIES:
        return
    for k in range(n):
        base = offset + 2 + k * 12
        if base + 12 > len(t):
            return
        tag, typ = struct.unpack(endian + "HH", t[base:base + 4])
        count = struct.unpack(endian + "I", t[base + 4:base + 8])[0]
        yield tag, typ, count, t[base + 8:base + 12]


def _ascii_value(t: bytes, endian: str, count: int, valfield: bytes) -> bytes | None:
    if count <= 0 or count > 64:
        return None
    if count <= 4:
        return valfield[:count]
    off = struct.unpack(endian + "I", valfield)[0]
    if off < 0 or off + count > len(t):
        return None
    return t[off:off + count]


def _parse_year(raw: bytes | None) -> int | None:
    if not raw or len(raw) < 4:
        return None
    try:
        year = int(raw[:4].decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None
    return year if 1900 <= year <= 2035 else None
