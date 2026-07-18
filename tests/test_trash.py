"""P21/C2 — the L18 amendment: trash.py is pure computation only (no
mutation lives here; see safeops.py for the actual kernel calls; the
staging-only guard is safeops._placement_error's dispose branch). Tests pass
an explicit uid= throughout to avoid os.getuid(), which doesn't exist on
Windows — trash_dirs_for only calls it when uid is omitted."""
from __future__ import annotations

import os

from mlo import trash


def test_home_trash_dirs_default_and_explicit(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    files_dir, info_dir = trash.home_trash_dirs(str(tmp_path))
    assert files_dir == str(tmp_path / "Trash" / "files")
    assert info_dir == str(tmp_path / "Trash" / "info")


def test_mount_trash_dirs_shape(tmp_path):
    files_dir, info_dir = trash.mount_trash_dirs(str(tmp_path), 1000)
    assert files_dir == str(tmp_path / ".Trash-1000" / "files")
    assert info_dir == str(tmp_path / ".Trash-1000" / "info")


def test_trash_dirs_for_prefers_home_when_same_device(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    src_dir = tmp_path / "home" / "staging"    # same tmp_path -> same st_dev
    src_dir.mkdir()
    src = src_dir / "f.mp3"
    src.write_bytes(b"x")
    files_dir, info_dir = trash.trash_dirs_for(str(src), data_home=str(home), uid=1000)
    assert files_dir == str(home / "Trash" / "files")


def test_unique_trash_name_no_collision_returns_basename(tmp_path):
    files = tmp_path / "files"
    info = tmp_path / "info"
    files.mkdir(); info.mkdir()
    assert trash.unique_trash_name(str(files), str(info), "song.mp3") == "song.mp3"


def test_unique_trash_name_collision_gets_numeric_suffix(tmp_path):
    files = tmp_path / "files"
    info = tmp_path / "info"
    files.mkdir(); info.mkdir()
    (files / "song.mp3").write_bytes(b"x")
    assert trash.unique_trash_name(str(files), str(info), "song.mp3") == "song (1).mp3"
    (files / "song (1).mp3").write_bytes(b"x")
    assert trash.unique_trash_name(str(files), str(info), "song.mp3") == "song (2).mp3"


def test_unique_trash_name_stale_info_entry_also_collides(tmp_path):
    """A leftover .trashinfo with no files/ sibling must push the name on —
    the exclusive .trashinfo create is the atomic claim in safeops._trash_posix,
    so a name whose info slot is taken would fail AFTER selection otherwise."""
    files = tmp_path / "files"
    info = tmp_path / "info"
    files.mkdir(); info.mkdir()
    (info / "song.mp3.trashinfo").write_text("stale")
    assert trash.unique_trash_name(str(files), str(info), "song.mp3") == "song (1).mp3"


def test_trashinfo_payload_shape():
    payload = trash.trashinfo("/mnt/staging/song.mp3", deleted_at="2026-07-17T10:00:00")
    assert payload == (
        "[Trash Info]\n"
        "Path=/mnt/staging/song.mp3\n"
        "DeletionDate=2026-07-17T10:00:00\n")


def test_trashinfo_percent_encodes_hostile_names():
    """XDG spec: Path= is percent-encoded — a newline or % in a filename
    must not corrupt the key-value format."""
    payload = trash.trashinfo("/mnt/s/a\nb%20.mp3", deleted_at="2026-07-17T10:00:00")
    assert "Path=/mnt/s/a%0Ab%2520.mp3\n" in payload
    assert payload.count("\n") == 3
