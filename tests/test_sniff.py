"""sniff.kind_of — magic-byte content detection, totality by property.

The evidence layer for the false-carve pile (S1): a recovery blob with a real
media header is routed by content, an extension that lies is overruled, and a
random blob is honestly "no kind" so it is never mis-hinted into a media tree.
"""
from __future__ import annotations

import struct

from hypothesis import given, settings, strategies as st

from mlo.sniff import kind_of, kind_of_bytes


def ftyp(brand: bytes, body: bytes = b"") -> bytes:
    """A minimal ISO base-media header: [size]['ftyp'][major brand]... ."""
    return struct.pack(">I", 24) + b"ftyp" + brand + b"\x00\x00\x00\x00" + body


def write(tmp_path, name: str, data: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


# ── video ────────────────────────────────────────────────────────────────────

def test_mp4_ftyp_is_video():
    assert kind_of_bytes(ftyp(b"isom")) == "video"
    assert kind_of_bytes(ftyp(b"mp42")) == "video"
    assert kind_of_bytes(ftyp(b"qt  ")) == "video"


def test_dat_carve_with_ftyp_routes_video(tmp_path):
    """S1 acceptance: a '.dat' blob whose header is ftyp is video, not junk —
    the extension is ignored, the content decides."""
    assert kind_of(write(tmp_path, "recovered_0007.dat", ftyp(b"mp42"))) == "video"


def test_swf_carve_with_flv_header_is_video(tmp_path):
    """T4's driving case: a recovery tool wrote FLV frames into a '.swf'."""
    assert kind_of(write(tmp_path, "recovered_0012.swf",
                         b"FLV\x01\x05\x00\x00\x00\x09")) == "video"


def test_matroska_avi_mpeg_asf_are_video():
    assert kind_of_bytes(b"\x1aE\xdf\xa3" + b"\x01" * 20) == "video"
    assert kind_of_bytes(b"RIFF\x00\x00\x00\x00AVI LIST") == "video"
    assert kind_of_bytes(b"\x00\x00\x01\xba" + b"\x21" * 20) == "video"   # MPEG PS
    assert kind_of_bytes(b"\x00\x00\x01\xb3" + b"\x21" * 20) == "video"   # MPEG video
    assert kind_of_bytes(b"\x30\x26\xb2\x75" + b"\x8e" * 20) == "video"   # ASF/WMV
    # VCD .DAT: MPEG wrapped in a RIFF-CDXA container (the Bond rips)
    assert kind_of_bytes(b"RIFF\x54\x7c\x00\x2cCDXAfmt ") == "video"


# ── audio ────────────────────────────────────────────────────────────────────

def test_wave_au_flac_ogg_mp3_are_audio():
    assert kind_of_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ") == "audio"
    assert kind_of_bytes(b".snd\x00\x00\x00\x18") == "audio"             # AU / .au
    assert kind_of_bytes(b"fLaC\x00\x00\x00\x22") == "audio"
    assert kind_of_bytes(b"OggS\x00\x02\x00\x00") == "audio"
    assert kind_of_bytes(b"ID3\x03\x00\x00\x00") == "audio"             # MP3 + ID3v2
    assert kind_of_bytes(b"\xff\xfb\x90\x00") == "audio"                # MP3 frame sync


def test_au_system_sound_carve(tmp_path):
    """A Java/JDK system sound saved bare ('.au'/'.snd') is audio, not junk."""
    assert kind_of(write(tmp_path, "beep.au", b".snd\x00\x00\x00\x18abcd")) == "audio"


def test_m4a_ftyp_is_audio_not_video():
    """Brand disambiguation: audio-only MP4 (M4A/M4B) must not route to Video."""
    assert kind_of_bytes(ftyp(b"M4A ")) == "audio"
    assert kind_of_bytes(ftyp(b"M4B ")) == "audio"


# ── image ────────────────────────────────────────────────────────────────────

def test_jpeg_png_gif_tiff_webp_bmp_ico_are_image():
    assert kind_of_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 10) == "image"  # JPEG
    assert kind_of_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10) == "image"
    assert kind_of_bytes(b"GIF89a" + b"\x00" * 10) == "image"
    assert kind_of_bytes(b"II*\x00" + b"\x08" * 10) == "image"          # TIFF (RAW too)
    assert kind_of_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image"
    assert kind_of_bytes(b"BM\x36\x00" + b"\x00" * 10) == "image"       # BMP
    assert kind_of_bytes(b"\x00\x00\x01\x00\x01\x00") == "image"        # ICO


def test_heic_ftyp_is_image():
    assert kind_of_bytes(ftyp(b"heic")) == "image"
    assert kind_of_bytes(ftyp(b"mif1")) == "image"


# ── honest "no kind": random blobs are never mis-hinted ──────────────────────

def test_random_bytes_are_not_a_photo(tmp_path):
    """S1 acceptance: a '.jpg' full of random bytes is NOT classified image —
    a wrong extension does not manufacture a media kind, and a headerless carve
    honestly gets no hint (it stays put, not laundered into a media tree)."""
    assert kind_of(write(tmp_path, "junk.jpg", b"\x03\x91\x2fgarbage-not-a-header")) is None
    assert kind_of_bytes(b"\x12\x34\x56\x78\x9a\xbc\xde\xf0") is None
    assert kind_of_bytes(b"random text, no magic") is None


def test_empty_short_and_missing_are_none(tmp_path):
    assert kind_of_bytes(b"") is None
    assert kind_of_bytes(b"F") is None
    assert kind_of(write(tmp_path, "empty.dat", b"")) is None
    assert kind_of(str(tmp_path / "does-not-exist.mp4")) is None


def test_ftyp_needs_the_box_not_just_the_word():
    """'ftyp' must be at offset 4 (a real box), not anywhere in the stream."""
    assert kind_of_bytes(b"xxxxftyp") == "video"          # brand slice is empty -> default video
    assert kind_of_bytes(b"ftyp\x00\x00\x00\x00") is None  # 'ftyp' at offset 0 is not a box


@settings(max_examples=200, deadline=None)
@given(st.binary(max_size=80))
def test_kind_of_bytes_is_total(data):
    out = kind_of_bytes(data)
    assert out is None or out in ("video", "audio", "image")


def test_never_media_exts_guard():
    """Log/db/executable extensions are guarded so a weak signature (an MP3
    frame sync in a .log) is never mislabeled a media carve by the sniff pass."""
    from mlo.sniff import NEVER_MEDIA_EXTS
    for e in (".log", ".cab", ".mof", ".dll", ".exe", ".msi", ".db"):
        assert e in NEVER_MEDIA_EXTS
