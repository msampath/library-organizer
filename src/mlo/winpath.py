r"""Windows-safe path handling, used by every module that touches a path.

Three concerns, solved once (defect L10):
  - long paths: the \\?\ prefix lifts the 260-char limit on Windows;
  - lone surrogates: Windows filenames may contain them; they round-trip through
    ``utf-8``/``surrogatepass`` bytes (SQLite TEXT would raise, so paths are stored
    as BLOBs built here);
  - drive identity: staging is same-drive by rule; ``drive_of`` is the default
    implementation and is *injectable* wherever it is consumed, so tests can
    simulate multiple drives inside one tmp directory.

Pure functions only. No filesystem mutation lives here (see test_architecture).
"""
from __future__ import annotations

import os

LONG_PREFIX = "\\\\?\\"
_UNC_LONG_PREFIX = "\\\\?\\UNC\\"


def is_windows() -> bool:
    return os.name == "nt"


def to_long(path: str) -> str:
    r"""Absolute path, \\?\-prefixed on Windows (no-op prefix elsewhere)."""
    if path.startswith(LONG_PREFIX):
        return path
    p = os.path.abspath(path)
    if not is_windows():
        return p
    if p.startswith("\\\\"):  # UNC \\server\share -> \\?\UNC\server\share
        return _UNC_LONG_PREFIX + p[2:]
    return LONG_PREFIX + p


def from_long(path: str) -> str:
    r"""The plain (human) form of a possibly \\?\-prefixed path."""
    if path.startswith(_UNC_LONG_PREFIX):
        return "\\\\" + path[len(_UNC_LONG_PREFIX):]
    if path.startswith(LONG_PREFIX):
        return path[len(LONG_PREFIX):]
    return path


def to_bytes(path: str) -> bytes:
    """Canonical lossless encoding for storage (BLOB columns)."""
    return from_long(path).encode("utf-8", "surrogatepass")


def from_bytes(data: bytes) -> str:
    return data.decode("utf-8", "surrogatepass")


def display(path: str) -> str:
    """Lossy, always-printable form for logs and *_display columns."""
    return from_long(path).encode("utf-8", "replace").decode("utf-8")


def drive_of(path: str) -> str:
    """Uppercase drive letter ('E') on Windows; '' when there is none (POSIX)."""
    drive, _ = os.path.splitdrive(os.path.abspath(from_long(path)))
    return drive.rstrip(":").upper() if drive and drive.endswith(":") else drive.upper()


def is_under(path: str, root: str) -> bool:
    """True if path == root or path is inside root (case-insensitive on Windows)."""
    p = os.path.abspath(from_long(path))
    r = os.path.abspath(from_long(root))
    if is_windows():
        p, r = os.path.normcase(p), os.path.normcase(r)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)
