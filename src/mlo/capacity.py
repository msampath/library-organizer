"""Free-space preflight (P21/C3): plan rows -> bytes needed per destination
volume vs bytes actually free there. Pure; the only I/O is a read-only
free-space query, never a mutation.

Only `copy_in` rows cost space — a `stage_move`/`move_within` is a rename
within the same volume (checked by the kernel's own same-volume placement
rule) and an `rmdir_empty` reclaims space, so neither can ever run this
volume short. `shutil` is banned repo-wide (test_architecture.py) even
though this is read-only, so the free-space query `shutil.disk_usage` would
normally do is reproduced directly with the same two-branch approach it uses
internally: GetDiskFreeSpaceExW on Windows, os.statvfs elsewhere.

A short volume produces a WARNING note, never a build refusal: unlike a
protected-path or coverage violation (structural, permanent until config
changes), free space can change between build and apply (an earlier section's
disposal frees room this one needs) — refusing to even show the plan would
hide the exact information the operator needs to decide."""
from __future__ import annotations

import os

from . import staging, winpath


def _volume_of(anchor: str, drive_of=None) -> str:
    """A volume identity for `anchor`: the drive letter/UNC share where one
    exists; st_dev elsewhere (POSIX); the anchor itself when unstatable."""
    drive = (drive_of or winpath.drive_of)(anchor)
    if drive:
        return drive
    try:
        return f"dev:{os.stat(winpath.to_long(anchor)).st_dev}"
    except OSError:
        return anchor


def bytes_required_by_volume(rows: list[dict], drive_of=None) -> dict[str, int]:
    """One representative dst anchor per VOLUME -> total bytes needed there,
    `copy_in` rows only. Aggregation is per volume, not per existing-ancestor
    directory: two destination folders on one disk share the same free pool,
    and splitting their requirements would let a combined shortfall pass
    unwarned (super-review M3)."""
    anchors: dict[str, str] = {}    # volume id -> representative anchor
    out: dict[str, int] = {}        # representative anchor -> bytes
    amemo: dict[str, str] = {}      # dirname -> nearest existing ancestor
    for r in rows:
        if r.get("kind") != "copy_in":
            continue
        size = (r.get("pre") or {}).get("size")
        if not size:
            continue
        d = os.path.dirname(r["dst"])
        anchor = amemo.get(d)
        if anchor is None:
            anchor = staging._nearest_existing_ancestor(r["dst"])
            amemo[d] = anchor
        rep = anchors.setdefault(_volume_of(anchor, drive_of), anchor)
        out[rep] = out.get(rep, 0) + size
    return out


def free_bytes(path: str) -> int | None:
    """Bytes free on the filesystem containing `path`. None if undeterminable
    (unreadable/unmounted) — callers must treat that as 'cannot verify', never
    as 'zero space' (which would false-alarm on every such path)."""
    try:
        if winpath.is_windows():
            import ctypes
            free = ctypes.c_ulonglong(0)
            ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(winpath.to_long(path)), ctypes.byref(free),
                None, None)
            return free.value if ok else None
        st = os.statvfs(winpath.to_long(path))
        return st.f_bavail * st.f_frsize
    except OSError:
        return None


def preflight_notes(rows: list[dict], drive_of=None) -> list[str]:
    """One WARNING note per destination volume that looks short on space for
    this plan; empty when everything fits or free space can't be determined
    (never a false alarm)."""
    notes = []
    for anchor, needed in sorted(bytes_required_by_volume(rows, drive_of).items()):
        free = free_bytes(anchor)
        if free is not None and needed > free:
            notes.append(
                f"WARNING: the volume of {anchor} needs {needed:,} bytes for "
                f"this plan but only {free:,} are free — apply may fail "
                f"partway through")
    return notes
