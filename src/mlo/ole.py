r"""Legacy OLE compound-file (CFB) property reader — the SummaryInformation and
DocumentSummaryInformation streams inside a .doc/.xls/.ppt, stdlib only, TOTAL.

`read(path) -> dict` returns any of {title, subject, author, keywords,
last_saved_by, company, manager, created, modified} for an OLE compound file,
or {} for anything else (a non-OLE file, or a corrupt one). It NEVER raises —
every malformed-structure path is caught and yields {}.

This is the legacy half of docmeta: the old binary Office files carry the same
author/title/company a human reads after opening them, just in a CFB container
instead of a ZIP. The format (MS-CFB + MS-OLEPS property sets) is parsed only as
far as the two summary streams need.
"""
from __future__ import annotations

import datetime
import struct

from . import winpath

_SIG = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF
_NOSTREAM = 0xFFFFFFFF

# Property IDs within each summary stream (MS-OLEPS). Keys match docmeta's OOXML
# schema so the two readers are interchangeable (author -> creator, last-saved-by
# -> last_modified_by).
_SUMMARY = {2: "title", 3: "subject", 4: "creator", 5: "keywords",
            8: "last_modified_by", 12: "created", 13: "modified"}
_DOC_SUMMARY = {14: "manager", 15: "company"}


def read(path: str) -> dict[str, str]:
    """Summary properties of an OLE compound file, or {}. Never raises."""
    try:
        with open(winpath.to_long(path), "rb") as f:
            data = f.read()
    except OSError:
        return {}
    if len(data) < 512 or data[:8] != _SIG:
        return {}
    try:
        return _parse(data)
    except Exception:                       # total by contract — malformed -> {}
        return {}


def _chain(data: bytes, fat: list[int], start: int, ssize: int) -> bytes:
    out, s, seen = [], start, set()
    while s not in (_ENDOFCHAIN, _FREESECT) and s < len(fat) and s not in seen:
        seen.add(s)
        off = (s + 1) * ssize
        out.append(data[off:off + ssize])
        s = fat[s]
    return b"".join(out)


def _parse(data: bytes) -> dict:
    ssize = 1 << struct.unpack_from("<H", data, 30)[0]
    dir_start = struct.unpack_from("<I", data, 48)[0]
    mini_cutoff = struct.unpack_from("<I", data, 56)[0]
    minifat_start = struct.unpack_from("<I", data, 60)[0]
    difat_start = struct.unpack_from("<I", data, 68)[0]
    difat = list(struct.unpack_from("<109I", data, 76))

    # Extend the DIFAT via its chain (large files only), bounded.
    s, guard = difat_start, 0
    per = ssize // 4
    while s not in (_ENDOFCHAIN, _FREESECT) and guard < 4096:
        guard += 1
        block = struct.unpack_from(f"<{per}I", data, (s + 1) * ssize)
        difat.extend(block[:-1])
        s = block[-1]

    fat: list[int] = []
    for fs in difat:
        if fs in (_ENDOFCHAIN, _FREESECT):
            continue
        fat.extend(struct.unpack_from(f"<{per}I", data, (fs + 1) * ssize))

    directory = _chain(data, fat, dir_start, ssize)
    entries = []                            # (name, type, start, size)
    for i in range(0, len(directory) - 127, 128):
        e = directory[i:i + 128]
        nlen = struct.unpack_from("<H", e, 64)[0]
        if not 0 < nlen <= 64:
            continue
        name = e[:nlen - 2].decode("utf-16-le", "ignore")
        entries.append((name, e[66], struct.unpack_from("<I", e, 116)[0],
                        struct.unpack_from("<I", e, 120)[0]))

    root = next((e for e in entries if e[1] == 5), None)   # root storage
    if root is None:
        return {}
    ministream = _chain(data, fat, root[2], ssize)
    minifat_raw = _chain(data, fat, minifat_start, ssize)
    minifat = list(struct.unpack_from(f"<{len(minifat_raw) // 4}I", minifat_raw)) \
        if minifat_raw else []
    msize = 1 << struct.unpack_from("<H", data, 32)[0]

    def stream(start: int, size: int) -> bytes:
        if size < mini_cutoff:              # small: mini-stream via the mini-FAT
            out, s2, seen = [], start, set()
            while s2 not in (_ENDOFCHAIN, _FREESECT) and s2 < len(minifat) \
                    and s2 not in seen:
                seen.add(s2)
                out.append(ministream[s2 * msize:s2 * msize + msize])
                s2 = minifat[s2]
            return b"".join(out)[:size]
        return _chain(data, fat, start, ssize)[:size]

    out: dict[str, str] = {}
    for sname, pids in (("\x05SummaryInformation", _SUMMARY),
                        ("\x05DocumentSummaryInformation", _DOC_SUMMARY)):
        ent = next((e for e in entries if e[0] == sname), None)
        if ent is not None:
            out.update(_property_set(stream(ent[2], ent[3]), pids))
    return out


def _property_set(blob: bytes, pids: dict[int, str]) -> dict:
    out: dict[str, str] = {}
    if len(blob) < 48 or struct.unpack_from("<H", blob, 0)[0] != 0xFFFE:
        return out
    if struct.unpack_from("<I", blob, 24)[0] < 1:       # section count
        return out
    sect = struct.unpack_from("<I", blob, 44)[0]        # first section offset
    if sect + 8 > len(blob):
        return out
    nprops = struct.unpack_from("<I", blob, sect + 4)[0]
    offsets: dict[int, int] = {}
    for i in range(min(nprops, 256)):
        pid, poff = struct.unpack_from("<II", blob, sect + 8 + i * 8)
        offsets[pid] = sect + poff

    codepage = 1252
    if 1 in offsets:
        cp = _value(blob, offsets[1])
        if isinstance(cp, int):
            cp &= 0xFFFF     # PID 1 is a signed VT_I2: CP 65001 reads as
                             # -535 and the UTF-8 mapping never fired
            codepage = 65001 if cp in (65001, 1200) else cp

    for pid, key in pids.items():
        if pid in offsets:
            v = _value(blob, offsets[pid], codepage)
            if isinstance(v, datetime.datetime):
                out[key] = v.strftime("%Y-%m-%d")
            elif v:
                out[key] = str(v)[:200]
    return out


def _value(blob: bytes, off: int, codepage: int = 1252):
    if off + 4 > len(blob):
        return None
    vt = struct.unpack_from("<I", blob, off)[0]
    p = off + 4
    if vt == 0x02:
        return struct.unpack_from("<h", blob, p)[0]
    if vt in (0x03, 0x16):
        return struct.unpack_from("<i", blob, p)[0]
    if vt == 0x1E:                          # VT_LPSTR (codepage string)
        ln = struct.unpack_from("<I", blob, p)[0]
        raw = blob[p + 4:p + 4 + ln].split(b"\x00", 1)[0]
        enc = "utf-8" if codepage == 65001 else f"cp{codepage}"
        try:
            return raw.decode(enc, "ignore").strip()
        except LookupError:
            return raw.decode("latin-1", "ignore").strip()
    if vt == 0x1F:                          # VT_LPWSTR (UTF-16)
        ln = struct.unpack_from("<I", blob, p)[0]
        return blob[p + 4:p + 4 + ln * 2].decode("utf-16-le", "ignore") \
            .split("\x00", 1)[0].strip()
    if vt == 0x40:                          # VT_FILETIME
        lo, hi = struct.unpack_from("<II", blob, p)
        ticks = (hi << 32) | lo
        if not ticks:
            return None
        return datetime.datetime(1601, 1, 1) + \
            datetime.timedelta(microseconds=ticks // 10)
    return None
