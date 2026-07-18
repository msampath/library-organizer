"""Magic-byte content sniffing — stdlib only, read-only, TOTAL.

kind_of() returns the media CONTENT kind of a file — 'video', 'audio', or
'image' — by reading its leading bytes, or None when the header matches no
known signature. It NEVER raises: an unreadable or headerless file is "no
kind", exactly as exif.year_of() returns "no year".

This is the answer to the false-carve pile: a recovery tool that wrote video
frames into a '.swf'/'.dat' blob, or Java system sounds saved as '.au', are
routed by what they ARE, not by an extension that lies. ('.img' and other
disk-image/archive extensions are deliberately on NEVER_MEDIA_EXTS below —
a real disk image whose first sector happens to match a media signature must
not be routed as media; the hints.py augmenter honors that list, while the
explicit `--sniff` CLI path sniffs whatever is in scope.) The kind is a
Hint (taxonomy.Hints.content_kind) the router consults ONLY when the extension
yields no bucket — content never overrides a configured extension, so this
adds evidence, it never contradicts the user's taxonomy.

Deliberate limits, all documented, none silent:
  - only the first 64 bytes are read; a container whose type-atom sits past that
    is "no kind" (the recovery carves this targets front-load their headers);
  - signatures are chosen specific — a 2-3 byte sig hitting random data is the
    accepted, vanishingly-rare false positive (a blob that opens 'BM' or 'FLV');
  - genuinely ambiguous containers take their most common kind and say so here:
    ASF is WMV *or* WMA (→ video, the common carve); Ogg is usually audio;
  - ISO base-media 'ftyp' is disambiguated by its major brand — M4A/M4B → audio,
    HEIC/HEIF/AVIF → image, everything else (MP4/MOV/3GP/QuickTime) → video.
"""
from __future__ import annotations

from . import winpath

_HEAD = 64
_KINDS = ("video", "audio", "image")

# Extensions that are DEFINITELY not media — a sniff caller skips them so a weak
# 2-3 byte signature (an MP3 frame sync, 'BM', '.snd') firing on a log/database/
# executable never mislabels it as a carve. (A 418 MiB '.log' that happens to
# open with valid MP3-frame bytes is the real case this prevents.)
NEVER_MEDIA_EXTS = frozenset({
    ".log", ".cab", ".mof", ".dll", ".exe", ".msi", ".sys", ".db", ".db3",
    ".sqlite", ".dl_", ".in_", ".cache", ".tmp", ".ini", ".xml", ".json",
    ".img", ".iso", ".ctl", ".lzma", ".gz", ".bz2", ".xz", ".z",   # disk images / archives / control files
})


def kind_of(path: str) -> str | None:
    """Content kind of the file at `path`, or None. Never raises."""
    try:
        with open(winpath.to_long(path), "rb") as f:
            head = f.read(_HEAD)
    except OSError:
        return None
    return kind_of_bytes(head)


def kind_of_bytes(b: bytes) -> str | None:
    """Content kind from leading bytes, or None. Pure and total."""
    if len(b) < 2:
        return None

    # ISO base-media family (MP4/MOV/M4A/HEIC/3GP): 'ftyp' box at offset 4, its
    # major brand at 8 decides audio vs image vs video.
    if b[4:8] == b"ftyp":
        return _iso_bmff_kind(b[8:12])

    # ── video ──────────────────────────────────────────────────────────────
    if b[:3] == b"FLV":                              # Flash video (the .swf carve)
        return "video"
    if b[:4] == b"\x1aE\xdf\xa3":                    # Matroska / WebM (EBML)
        return "video"
    if b[:4] == b"RIFF" and b[8:12] == b"AVI ":
        return "video"
    if b[:4] == b"RIFF" and b[8:12] == b"CDXA":      # VCD: MPEG in a RIFF-CDXA wrapper (.DAT)
        return "video"
    if b[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"):  # MPEG PS / MPEG-1/2
        return "video"
    if b[:4] == b"\x30\x26\xb2\x75":                 # ASF (WMV; WMA shares it)
        return "video"

    # ── audio ──────────────────────────────────────────────────────────────
    if b[:4] == b"RIFF" and b[8:12] == b"WAVE":
        return "audio"
    if b[:4] == b".snd":                             # NeXT/Sun AU (.au/.snd; Java sounds)
        return "audio"
    if b[:4] == b"fLaC":
        return "audio"
    if b[:4] == b"OggS":                             # Ogg (Vorbis/Opus — audio)
        return "audio"
    if b[:3] == b"ID3":                              # MP3 with an ID3v2 tag
        return "audio"
    if b[0] == 0xFF and (b[1] & 0xE0) == 0xE0:       # MPEG audio frame sync (MP3/MP2)
        return "audio"

    # ── image ──────────────────────────────────────────────────────────────
    if b[:3] == b"\xff\xd8\xff":                     # JPEG
        return "image"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image"
    if b[:4] in (b"II*\x00", b"MM\x00*"):            # TIFF (and TIFF-based RAW)
        return "image"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image"
    if b[:2] == b"BM":                               # BMP (weak 2-byte sig)
        return "image"
    if b[:4] == b"\x00\x00\x01\x00":                 # ICO (icons)
        return "image"

    return None


def _iso_bmff_kind(brand: bytes) -> str:
    """Map an ISO base-media major brand to a content kind. Unknown brands are
    video — the overwhelming majority of ftyp carves are MP4/MOV footage."""
    if brand[:3] in (b"M4A", b"M4B", b"M4P", b"F4A", b"F4B"):
        return "audio"
    if brand in (b"heic", b"heix", b"heif", b"mif1", b"msf1", b"avif", b"avis"):
        return "image"
    return "video"
