"""Semantic containers (C33): subtrees that mean something as a UNIT.

A container is a subtree whose folder name declares its meaning — a phone
backup snapshot, a full-drive image, an app-data export. Two properties make
it a container: IDENTITY (the subtree means something as a whole) and
INTEGRITY (splitting it destroys the meaning — a Contacts_005.vcf apart from
its snapshot siblings is an orphan). Every per-file mechanism in mlo must
therefore keep its hands off container members; `plan.build_containers` moves
the whole subtree to the kind's home, structure intact, or not at all.

The four-way subtree triage this module anchors (C33):
  curated tree  -> stay put (C15/C18 already-placed guards)
  container     -> move WHOLE subtree to its home (this module + build_containers)
  dump          -> strip the wrapper segment, route files per-file (C27 flatten)
  loose files   -> route per-file (reorganize / date-drain)
Ownership order: curated -> container -> dump -> loose. Container detection
runs before flatten and before per-file routing.

PURE: no I/O, no filesystem, imports nothing from mlo (so taxonomy and config
may both import it without cycles). Pattern strings are searched with
re.IGNORECASE via the stdlib regex cache — the audioclass precedent.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# The media top-level labels. Canonical home of this constant (taxonomy
# re-exports it): media buckets are media taxonomy's territory — containers
# (C33) and flatten (C28) both stop at this boundary.
MEDIA_LABELS = frozenset({"Video", "Videos", "Audio", "Photos", "Images"})

# A path segment that is provenance/backup scaffolding, never a real album —
# drive-letter prefixes (E_NAS1, H_ALL), part markers (HDD2_Part1), and
# backup/device names. Canonical home of this constant (taxonomy re-exports).
PROVENANCE_SEG = re.compile(
    r"^[A-Za-z]_"                 # E_NAS1, G_Phone2, H_ALL
    r"|[ _-]part[ _-]?\d"         # HDD2_Part1, ... Part 2
    # HDD2Backup, WorkLaptop, OldThumbDrive — but NOT Thumbnails
    # (validator-A false positive: bare 'thumb' matched every thumbnail dir)
    r"|backup|laptop|thumb(?!nail)|\bhdd\b",
    re.IGNORECASE)

# Built-in container kinds: kind -> folder-segment patterns. Config
# [containers.patterns] entries are consulted BEFORE these (audioclass
# `extra` precedent); [containers.homes] entries override _HOMES.
# Dict order IS the kind-priority order root_of iterates (D3, owner
# correction #4): phone-backup > app-backup > drive-image.
_PATTERNS: dict[str, tuple[str, ...]] = {
    "phone-backup": (
        r"phone\s*backups?",                       # Phone Backup / Phone Backups
        r"(?:nexus|pixel|galaxy)\s*\d*\s*backup",  # Nexus6 Backup, Pixel8 backup
        r"^s\s?\d+\s*backup$",                     # S5 backup, S 5 backup
        r"^note\s?\d+\s*backup$",
    ),
    "app-backup": (
        r"\w+backup$",                             # User1Backup, User1Backup
        r"^backups?$",                             # a bare backup/ folder
        r"^backups?\s+of\b",                       # Backup of thesis
    ),
    "drive-image": (
        r"^[a-z]\s*drive$",                        # D drive
        r"^drive\s*[a-z]$",                        # Drive D
    ),
}
# The KIND names the destination tree; the container's own name only
# disambiguates inside it (owner correction #2, 2026-07-11: four phone
# backups named 'Phone Backups', 'CellPhone Backups', 'User1Backup' and
# 'Phone backup' must land in ONE tree, not four accidental siblings).
_HOMES: dict[str, str] = {
    "phone-backup": "Backups/Phones",
    "drive-image": "Backups/Drives",
    "app-backup": "Backups/Apps",
}

# Device patterns — a path segment that names a phone/tablet model. Match
# order matters: specific families first (a plain 's<n>' is a Samsung Galaxy;
# 'oneplus' would otherwise be shadowed if reordered). Each entry returns the
# NORMALIZED device name — 'S4backup' collapses to 'S4' so scattered backups
# of the same device (S4, S4backup, s4Backup) merge into one tree.
_DEVICE_PATTERNS: tuple[tuple[re.Pattern, "callable"], ...] = (
    # Samsung Galaxy S1–S99, optional 'backup' suffix normalized away
    (re.compile(r"^s\s?([1-9]\d?)(?:\s*backup)?$", re.IGNORECASE),
     lambda m: f"S{m.group(1)}"),
    # Samsung Galaxy Note
    (re.compile(r"^note\s?([1-9]\d?)$", re.IGNORECASE),
     lambda m: f"Note{m.group(1)}"),
    # Samsung Galaxy Tab
    (re.compile(r"^tab\s?([1-9]\d?)$", re.IGNORECASE),
     lambda m: f"Tab{m.group(1)}"),
    # Google Nexus (4, 5, 5X, 6, 6P, 7, 9, 10)
    (re.compile(r"^nexus\s?(\d+[a-z]?)$", re.IGNORECASE),
     lambda m: f"Nexus{m.group(1)}"),
    # Google Pixel — Pixel, Pixel8, Pixel8Pro/XL/a
    (re.compile(r"^pixel\s?(\d+)?\s?(pro|xl|a)?$", re.IGNORECASE),
     lambda m: "Pixel"
        + (m.group(1) or "")
        + ((m.group(2) or "").upper() if m.group(2) and m.group(2).lower() == "xl"
           else (m.group(2) or "").title())),
    # iPhone — iPhone5, iPhoneSE, iPhone11Pro, iPhoneXR
    (re.compile(r"^iphone\s?(\d+|se|xr?|xs)?\s?(pro|max|plus|mini)?$",
                re.IGNORECASE),
     lambda m: "iPhone"
        + (m.group(1) or "").upper()
        + (m.group(2) or "").title()),
    # iPad (Air/Pro/Mini) N
    (re.compile(r"^ipad\s?(air|pro|mini)?\s?(\d+)?$", re.IGNORECASE),
     lambda m: "iPad"
        + ((" " + m.group(1).title()) if m.group(1) else "")
        + (m.group(2) or "")),
    # OnePlus
    (re.compile(r"^oneplus\s?(\d+\w*)?$", re.IGNORECASE),
     lambda m: "OnePlus" + (m.group(1) or "")),
    # HTC (One, 10, U11 …)
    (re.compile(r"^htc\s?(\w+)$", re.IGNORECASE),
     lambda m: f"HTC {m.group(1)}"),
    # Sony Xperia
    (re.compile(r"^xperia\s?(\w+)?$", re.IGNORECASE),
     lambda m: "Xperia" + ((" " + m.group(1)) if m.group(1) else "")),
    # Xiaomi / Redmi
    (re.compile(r"^(?:redmi|xiaomi)\s?(\w+)$", re.IGNORECASE),
     lambda m: f"Redmi {m.group(1)}"),
    # Motorola Moto
    (re.compile(r"^moto\s?(\w+)?$", re.IGNORECASE),
     lambda m: "Moto" + ((" " + m.group(1)) if m.group(1) else "")),
    # LG (a model number always follows)
    (re.compile(r"^lg\s?(\w+)$", re.IGNORECASE),
     lambda m: f"LG {m.group(1)}"),
)


def find_device(seg: str) -> str | None:
    """Return the NORMALIZED device name if `seg` names a phone/tablet model,
    else None. Normalization is what merges scattered backups of the same
    device: 'S4backup' -> 'S4', 'note 10' -> 'Note10', 's5' -> 'S5'. A segment
    that isn't a device (an owner name, an app-data folder) returns None."""
    if not seg:
        return None
    for pat, normalize in _DEVICE_PATTERNS:
        m = pat.match(seg.strip())
        if m:
            return normalize(m)
    return None


def owner_of(cfg, container_root: str) -> str | None:
    """The 'owner' segment between the top bucket and the container root — a
    person or alias (`user1`, `User2`, `User3`) that names WHOSE backup this
    is. Provenance drive-prefixes (`I_SSD1`, `E_NAS1`) are skipped.
    Used as the discriminator when device-keyed destinations collide with
    different content (D12): `home\\<device>\\<owner>\\<below>`. None when the
    container root sits directly under the bucket."""
    segs = container_root.replace("/", os.sep).split(os.sep)
    # segments between the bucket (segs[0]) and the root (segs[-1])
    between = segs[1:-1]
    for seg in reversed(between):
        if not PROVENANCE_SEG.search(seg):
            return seg
    return None


@dataclass(frozen=True)
class ContainerMatch:
    root: str        # native-sep relpath of the container root directory
    kind: str        # container kind (pattern table key)
    home: str        # destination root, native-sep relative path


def builtin_homes() -> dict[str, str]:
    return dict(_HOMES)


def home_for(cfg, kind: str) -> str | None:
    """The merged (config-over-builtin) home for a kind, native separators."""
    home = {**_HOMES, **getattr(cfg, "container_homes", {})}.get(kind)
    return home.replace("/", os.sep) if home else None


def _merged(cfg) -> tuple[list[tuple[str, tuple[str, ...]]], dict[str, str]]:
    """(ordered kind->patterns list, merged homes). Config entries first so a
    user's convention outranks the built-ins; a kind is actionable only when
    the merged homes table names its destination (validated at config load)."""
    pats = list(getattr(cfg, "container_patterns", {}).items()) \
        + [(k, v) for k, v in _PATTERNS.items()
           if k not in getattr(cfg, "container_patterns", {})]
    homes = {**_HOMES, **getattr(cfg, "container_homes", {})}
    return pats, homes


def root_of(cfg, relpath: str) -> ContainerMatch | None:
    """The container claiming `relpath`, or None.

    Scope rules (C33): segment 0 (the top bucket) is never a container root
    and must NOT be a media label (D2 — media taxonomy owns its territory);
    the filename is never a container root.

    Selection rules (D3, owner correction #4 2026-07-11): KIND PRIORITY
    outranks segment position — phone-backup > app-backup > drive-image. A
    phone backup nested inside a drive image (`Documents\\D drive\\Phone
    backup\\WhatsApp Documents\\…`) belongs to the phone-backup, because the
    drive image is just an accidental wrapping around it; the previous
    'outermost wins' rule buried these under `Backups\\Drives`. Within one
    kind, outermost still wins (a phone backup inside another phone backup
    belongs to the outer one).

    The path between bucket and root carries no meaning to the destination
    (owner corrections #1-#3): person/drive segments ('user1', 'I_SSD1')
    are provenance, dropped by build_containers when computing the destination."""
    segs = relpath.replace("/", os.sep).split(os.sep)
    if len(segs) < 3:                       # bucket / root-seg / file minimum
        return None
    if segs[0].casefold() in {m.casefold() for m in MEDIA_LABELS}:
        return None
    pats, homes = _merged(cfg)
    # At-home claim (C39): any path under a container HOME is a container
    # member forever — `<home>\<ident>\…` is claimed with root <home>\<ident>,
    # BEFORE the pattern scan. The live library proved the alternative: once
    # `…\Phone Backups\…` became `Backups\Phones\Nexus6\…` no phone-backup
    # pattern matched the new path, so reorganize (C35 routes), flatten (C34)
    # and build_containers itself (re-claiming the inner `InkPad_Notepad\
    # backup` folders as app-backups) all tore files back out of the executed
    # snapshot — 19 of them landed in a comingled pile the owner then deleted
    # as junk. Longest home wins so a more specific configured home outranks
    # a broader one.
    for kind, home in sorted(homes.items(), key=lambda kv: -len(kv[1])):
        hsegs = home.replace("/", os.sep).split(os.sep)
        if len(segs) >= len(hsegs) + 2 and all(
                s.casefold() == h.casefold()
                for s, h in zip(segs, hsegs)):
            return ContainerMatch(
                root=os.sep.join(segs[:len(hsegs) + 1]), kind=kind,
                home=home.replace("/", os.sep))
    # Kind-priority order: iterate patterns' kinds first so a phone-backup
    # anywhere in the path beats a drive-image at the outermost position.
    for kind, patterns in pats:
        home = homes.get(kind)
        if home is None:
            continue
        for i in range(1, len(segs) - 1):    # within a kind, outermost wins
            seg = segs[i]
            for p in patterns:
                if re.search(p, seg, re.IGNORECASE):
                    return ContainerMatch(
                        root=os.sep.join(segs[:i + 1]), kind=kind,
                        home=home.replace("/", os.sep))
    return None
