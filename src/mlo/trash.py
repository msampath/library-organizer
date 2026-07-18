"""Disposal (P21/C2 — the L18 amendment): pure computation only.

The staging-only guard lives in the kernel (`safeops._placement_error`'s
dispose branch), not here. The POSIX helpers below compute WHERE a file's
XDG trash entry belongs; they read the filesystem (os.stat/os.path.exists —
read-only) but never write or move anything. The actual OS-level
recycle/trash call (the Windows Recycle Bin API, POSIX os.rename + writing
.trashinfo) lives in safeops.py, the sole kernel (L0) — nothing here
mutates.
"""
from __future__ import annotations

import os
import time
import urllib.parse

from . import staging, winpath


# ── POSIX XDG trash ──────────────────────────────────────────────────────────
# No delete primitive exists in this codebase, so a trash move must never be
# cross-device (a cross-device "move" would need a copy + remove-the-original,
# and there is no remove). XDG's own answer to this is per-mount trash
# directories; trash_dirs_for prefers one whenever the home trash would
# cross a device boundary, so the kernel's move is always a plain same-device
# os.rename.

def home_trash_dirs(data_home: str | None = None) -> tuple[str, str]:
    base = data_home or os.environ.get("XDG_DATA_HOME") \
        or os.path.join(os.path.expanduser("~"), ".local", "share")
    root = os.path.join(base, "Trash")
    return os.path.join(root, "files"), os.path.join(root, "info")


def mount_trash_dirs(mount_root: str, uid: int) -> tuple[str, str]:
    root = os.path.join(mount_root, f".Trash-{uid}")
    return os.path.join(root, "files"), os.path.join(root, "info")


def _dev_of(path: str) -> int | None:
    try:
        return os.stat(winpath.to_long(staging._nearest_existing_ancestor(path))).st_dev
    except OSError:
        return None


def _mount_root_of(path: str) -> str:
    """The outermost existing ancestor sharing `path`'s device — a good-
    enough approximation of its filesystem mount point to keep the trash
    move same-device."""
    p = staging._nearest_existing_ancestor(path)
    dev = _dev_of(p)
    while True:
        parent = os.path.dirname(p)
        if parent == p or _dev_of(parent) != dev:
            return p
        p = parent


def trash_dirs_for(src: str, *, data_home: str | None = None,
                   uid: int | None = None) -> tuple[str, str]:
    """(files_dir, info_dir): the home XDG trash when it shares `src`'s
    device (the common case), else a per-mount `.Trash-<uid>` at src's own
    mount point."""
    files_dir, info_dir = home_trash_dirs(data_home)
    src_dev = _dev_of(src)
    if src_dev is not None and src_dev == _dev_of(os.path.dirname(files_dir)):
        return files_dir, info_dir
    resolved_uid = uid if uid is not None else os.getuid()
    return mount_trash_dirs(_mount_root_of(src), resolved_uid)


def unique_trash_name(files_dir: str, info_dir: str, basename: str) -> str:
    """A name free in BOTH files/ and info/ — trash accumulation naming
    ('file', 'file (1)', 'file (2)'...) is standard OS trash behavior,
    unrelated to mlo's own no-silent-rename law for LIBRARY placement (L17):
    that law exists to stop duplicate accumulation in the curated library,
    not in a bin the user already knows is disposable. Checking info/ too
    matters because the .trashinfo exclusive-create is the atomic claim
    (safeops._trash_posix) — a stale info/ entry must not be able to make
    that claim fail AFTER a name was chosen."""
    stem, ext = os.path.splitext(basename)
    name, n = basename, 1
    while (os.path.exists(os.path.join(files_dir, name))
           or os.path.exists(os.path.join(info_dir, name + ".trashinfo"))):
        name = f"{stem} ({n}){ext}"
        n += 1
    return name


def trashinfo(original_path: str, deleted_at: str | None = None) -> str:
    """The .trashinfo payload (XDG spec): original absolute path
    (percent-encoded per the spec, so newlines/%-chars in a filename cannot
    corrupt the key-value format) + deletion timestamp, so a desktop trash
    UI can show/restore it correctly."""
    return ("[Trash Info]\n"
           f"Path={urllib.parse.quote(original_path, safe='/')}\n"
           f"DeletionDate={deleted_at or time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
