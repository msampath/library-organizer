r"""Image pre-classifier — deterministic 'not all images are photos' triage.

Parallel to audioclass: before a nameless carve is punted to a vision model or
the Unsorted bin, this classifies it BY NAME so the large fraction that was
never a personal photo routes for free. A real 9,073-file dump measured 35%
WhatsApp, ~20% web/UI graphics (mostly .gif sprites, spinners, logos), 13%
epoch-timestamp-named, 2% screenshots — only ~18% true carves:

  'whatsapp'    WhatsApp images (IMG-YYYYMMDD-WA####) -> Images\WhatsApp
  'ui'          web/app UI graphics — .gif sprites, spinners, icons, logos,
                button/asset PNGs -> Images\Graphics_Icons
  'screenshot'  screen captures -> Images\Screenshots
  None          a photo with no special class -> the normal photo branch
                (EXIF year, else a name-derived year -> Photos\<Year>, else the
                Unsorted bin)

First match wins; `extra` (from config) is consulted before the built-ins so a
user's convention overrides. Name-only, like audioclass — content sniffing and
EXIF stay upstream in the caller's Hints.
"""
from __future__ import annotations

import datetime
import os
import re

# WhatsApp image naming (the '-WA' suffix is WhatsApp-specific).
_WHATSAPP = (
    r"^IMG-\d{8}-WA\d",
)

# Screen captures across Android / iOS / Windows / macOS conventions.
_SCREENSHOT = (
    r"screen[ _-]?shot",
    r"scrnshot",
)

# Web / app UI graphics: never personal photos. The .gif extension is caught by
# the caller (below); these name fragments catch the PNG/JPG UI assets.
_UI = (
    r"(^|[ _-])(loading|spinner|preloader)([ _.-]|$)",
    r"(^|[ _-])(icon|logo|button|btn|badge|banner|sprite|avatar)([ _.-]|$)",
    r"(^|[ _-])(splash|setup|installer|wizard)([ _.-]|$)",
    r"_(normal|pressed|selected|disabled|hover|active|thumb)\.",
    r"^(actionbar|toolbar|tab|nav|menu|header|footer)[ _-]",
)

_CLASSES = (("whatsapp", _WHATSAPP), ("screenshot", _SCREENSHOT), ("ui", _UI))

# 13-digit epoch milliseconds (leading 1 -> 2001..2033) as the whole stem: a
# camera/app dump name that is really a capture timestamp.
_EPOCH_MS = re.compile(r"^(1\d{12})$")


def classify(basename: str,
             extra: dict[str, tuple[str, ...]] | None = None) -> str | None:
    """Deterministic image kind: 'whatsapp' | 'ui' | 'screenshot' | None.
    None means 'ordinary photo' — the caller applies year/Unsorted placement."""
    name = basename.replace("\\", "/").rsplit("/", 1)[-1]
    for kind, patterns in list((extra or {}).items()) + list(_CLASSES):
        for p in patterns:
            if re.search(p, name, re.IGNORECASE):
                return kind
    if os.path.splitext(name)[1].lower() == ".gif":     # animated web/UI sprite
        return "ui"
    return None


_STRUCTURED_WA = re.compile(r"^(?:VID|IMG)-(\d{4})(\d{2})(\d{2})-WA\d")
_STRUCTURED_DEVICE_STAMP = re.compile(r"^(\d{4})(\d{2})(\d{2})\d{6}[_.]")


def structured_name_year(basename: str) -> int | None:
    """A STRONGLY-structured name date — narrower than name_year below, and
    deliberately outranks embedded container metadata (P18/C45: a WhatsApp
    re-encode writes a bogus constant mvhd date, but the filename the device
    wrote is trustworthy). Matches only two unambiguous device conventions:
    a WhatsApp `VID-YYYYMMDD-WA####` / `IMG-YYYYMMDD-WA####` capture, or a
    leading 14-digit `YYYYMMDDHHMMSS` device stamp (dashcam
    `20250520050008_...`). Year/month/day are range-checked; a bogus date
    (month 13, day 40) or an implausible year yields None like name_year."""
    name = basename.replace("\\", "/").rsplit("/", 1)[-1]
    m = _STRUCTURED_WA.match(name) or _STRUCTURED_DEVICE_STAMP.match(name)
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return year if 1990 <= year <= 2035 else None


def name_year(basename: str) -> int | None:
    """Capture year derived from the FILENAME alone, or None. Only the
    unambiguous case: a 13-digit epoch-ms stem (`1493582779771.jpg`). Used as a
    fallback when EXIF gave no year, so a timestamp-named photo lands in
    Photos\\<Year> instead of the Unsorted shelf."""
    stem = os.path.splitext(basename.replace("\\", "/").rsplit("/", 1)[-1])[0]
    m = _EPOCH_MS.match(stem)
    if not m:
        return None
    year = datetime.datetime.fromtimestamp(
        int(m.group(1)) / 1000, datetime.timezone.utc).year
    return year if 2000 <= year <= 2035 else None
