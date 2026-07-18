"""staging.py — resolution of [staging] roots beyond single drive letters
(P21/A4, the B1 blocker). POSIX behavior is exercised via an INJECTED
drive_of that always returns '' — the same fake-drive testing pattern
conftest.py uses for Windows drive letters, applied to prove the POSIX
code path on any host, not just real POSIX CI."""
from __future__ import annotations

import os

from mlo import staging


def _posix_drive_of(path: str) -> str:
    return ""


# ── key-shape predicates ─────────────────────────────────────────────────

def test_is_drive_letter_key():
    assert staging.is_drive_letter_key("E")
    assert staging.is_drive_letter_key("z")
    assert not staging.is_drive_letter_key("EE")
    assert not staging.is_drive_letter_key("1")
    assert not staging.is_drive_letter_key("")


def test_is_staging_prefix_key():
    assert staging.is_staging_prefix_key(r"\\NAS\Share")
    assert staging.is_staging_prefix_key("//nas/share")
    assert staging.is_staging_prefix_key("/mnt/media")
    assert not staging.is_staging_prefix_key("E")
    assert not staging.is_staging_prefix_key("relative/path")


# ── root_for: drive-letter fast path (back-compat, unchanged) ──────────────

def test_root_for_drive_letter_exact_match():
    cfg = {"E": r"E:\Delete-mlo"}
    assert staging.root_for(cfg, r"E:\stuff\a.txt", lambda p: "E") \
        == r"E:\Delete-mlo"


def test_root_for_drive_letter_no_match_returns_none():
    cfg = {"E": r"E:\Delete-mlo"}
    assert staging.root_for(cfg, r"F:\stuff\a.txt", lambda p: "F") is None


def test_root_for_unc_share_resolves_via_exact_drive_of_match():
    # drive_of already returns the whole UNC share as one atomic string —
    # a plain exact match, no prefix-walk needed.
    cfg = {r"\\NAS\SHARE": r"\\NAS\SHARE\Delete-mlo"}
    assert staging.root_for(cfg, r"\\NAS\SHARE\movies\a.mkv",
                            lambda p: r"\\NAS\SHARE") \
        == r"\\NAS\SHARE\Delete-mlo"


def test_root_for_returns_none_when_staging_empty():
    assert staging.root_for({}, "/anything", _posix_drive_of) is None


# ── root_for: absolute-path-prefix match — the actual B1/POSIX fix ─────────

def test_root_for_posix_prefix_match(tmp_path):
    mount = tmp_path / "mnt" / "media"
    stage = mount / "Delete-mlo"
    cfg = {str(mount): str(stage)}
    inner = str(mount / "a" / "b.txt")
    assert staging.root_for(cfg, inner, _posix_drive_of) == str(stage)


def test_root_for_posix_prefix_no_match_returns_none(tmp_path):
    mount = tmp_path / "mnt" / "media"
    other = tmp_path / "elsewhere"
    cfg = {str(mount): str(mount / "Delete-mlo")}
    assert staging.root_for(cfg, str(other / "f.txt"), _posix_drive_of) is None


def test_root_for_posix_longest_prefix_wins(tmp_path):
    outer = tmp_path / "mnt"
    inner = tmp_path / "mnt" / "media"
    cfg = {str(outer): str(outer / "Delete-outer"),
           str(inner): str(inner / "Delete-inner")}
    path = str(inner / "x.txt")
    assert staging.root_for(cfg, path, _posix_drive_of) \
        == str(inner / "Delete-inner")


def test_root_for_prefix_key_exact_root_itself_matches(tmp_path):
    mount = tmp_path / "mnt" / "media"
    cfg = {str(mount): str(mount / "Delete-mlo")}
    assert staging.root_for(cfg, str(mount), _posix_drive_of) \
        == str(mount / "Delete-mlo")


# ── same_volume ──────────────────────────────────────────────────────────

def test_same_volume_drive_letter_identity():
    drive_of = lambda p: "E" if p.startswith("E") else "I"
    assert staging.same_volume("E:\\a", "E:\\b", drive_of) is True
    assert staging.same_volume("E:\\a", "I:\\b", drive_of) is False


def test_same_volume_posix_same_filesystem_via_nearest_ancestor(tmp_path):
    a_dir = tmp_path / "vol" / "a"
    b_dir = tmp_path / "vol" / "b"
    a_dir.mkdir(parents=True)
    b_dir.mkdir(parents=True)
    a_target = str(a_dir / "not-yet-created" / "dst.bin")
    b_target = str(b_dir / "also-not-yet-created" / "dst.bin")
    assert staging.same_volume(a_target, b_target, _posix_drive_of) is True


def test_same_volume_posix_resolves_missing_dst_to_nearest_existing_ancestor(
        tmp_path):
    """The st_dev comparison must run on the nearest EXISTING ancestor —
    a not-yet-created destination resolves to its nearest real parent, so
    same_volume gives a real answer instead of falling into the
    unprovable-OSError default. (Not pinned by monkeypatching os.stat: the
    existence walk itself is stat-based on POSIX, so a global stat guard
    can only recurse or misfire — it also breaks pytest's own reporting.)"""
    target = str(tmp_path / "deep" / "not" / "real" / "dst.bin")
    resolved = staging._nearest_existing_ancestor(target)
    assert os.path.exists(resolved)
    assert os.path.samefile(resolved, str(tmp_path))
    assert staging.same_volume(target, str(tmp_path), _posix_drive_of) is True


def test_same_volume_posix_unreadable_defaults_true():
    assert staging.same_volume("/does/not/exist/a", "/does/not/exist/b",
                               _posix_drive_of) is True
