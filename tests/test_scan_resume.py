"""Scanner: stat fast-path resume, pruning, artifact stamping."""
from __future__ import annotations

import os

import pytest

from conftest import make_file
from helpers import make_cfg
from mlo import fingerprint, scan
from mlo.config import ConfigError


def seed_library(world, n=3):
    files = []
    for i in range(n):
        files.append(make_file(world["lib"] / "Audio" / f"track{i}.mp3",
                               b"x" * (100 + i)))
    return files


def test_first_scan_indexes_all(world):
    cfg = make_cfg(world)
    seed_library(world, 3)
    n, skipped = scan.scan_library(world["store"], cfg, "run-1")
    assert (n, skipped) == (3, [])
    assert world["store"].index_count() == 3
    assert world["store"].artifact_fresh("index:library", cfg.config_hash)


def test_unchanged_rescan_hashes_nothing(world, monkeypatch):
    cfg = make_cfg(world)
    seed_library(world, 3)
    scan.scan_library(world["store"], cfg, "run-1")

    def boom(path):
        raise AssertionError(f"re-hashed unchanged file: {path}")

    monkeypatch.setattr(fingerprint, "quick", boom)
    n, skipped = scan.scan_library(world["store"], cfg, "run-2")
    assert (n, skipped) == (0, [])


def test_changed_file_rehashed_alone(world):
    cfg = make_cfg(world)
    files = seed_library(world, 3)
    scan.scan_library(world["store"], cfg, "run-1")
    files[1].write_bytes(b"totally different and longer content")
    n, _ = scan.scan_library(world["store"], cfg, "run-2")
    assert n == 1
    rel = os.path.relpath(str(files[1]), str(world["lib"]))
    assert world["store"].index_get(rel)["size"] == len(
        b"totally different and longer content")


def test_new_file_appended(world):
    cfg = make_cfg(world)
    seed_library(world, 2)
    scan.scan_library(world["store"], cfg, "run-1")
    make_file(world["lib"] / "Video" / "new.mp4", b"v" * 50)
    n, _ = scan.scan_library(world["store"], cfg, "run-2")
    assert n == 1
    assert world["store"].index_count() == 3


def test_bluestacks_dir_pruned(world):
    cfg = make_cfg(world)
    make_file(world["lib"] / "OLD_BlueStacks_bkp" / "disk.img", b"B" * 10)
    make_file(world["lib"] / "Audio" / "ok.mp3", b"A" * 10)
    n, _ = scan.scan_library(world["store"], cfg, "run-1")
    assert n == 1
    assert world["store"].index_get(
        os.path.join("OLD_BlueStacks_bkp", "disk.img")) is None


def test_unreadable_file_lands_in_skipped(world, monkeypatch):
    cfg = make_cfg(world)
    good = make_file(world["lib"] / "Audio" / "good.mp3", b"g")
    bad = make_file(world["lib"] / "Audio" / "bad.mp3", b"b")
    real = fingerprint.quick

    def flaky(path):
        if path.endswith("bad.mp3"):
            raise OSError("locked")
        return real(path)

    monkeypatch.setattr(fingerprint, "quick", flaky)
    n, skipped = scan.scan_library(world["store"], cfg, "run-1")
    assert n == 1
    assert len(skipped) == 1 and skipped[0].endswith("bad.mp3")
    assert good.exists() and bad.exists()


def test_source_scan_drops_rows_for_deleted_files(world):
    cfg = make_cfg(world)
    keep = make_file(world["E"] / "photos" / "keep.mp3", b"K" * 20)
    gone = make_file(world["E"] / "photos" / "gone.mp3", b"G" * 20)
    n, _ = scan.scan_source(world["store"], cfg, "e", "run-1")
    assert n == 2
    gone.unlink()
    n, _ = scan.scan_source(world["store"], cfg, "e", "run-2")
    assert n == 1
    rows = list(world["store"].source_iter("e"))
    assert [r["relpath"] for r in rows] == [
        os.path.relpath(str(keep), str(world["E"]))]


def test_disabled_source_refused(world):
    from mlo.config import Source
    cfg = make_cfg(world, sources=(Source("e", str(world["E"]), False),))
    with pytest.raises(ConfigError, match="disabled"):
        scan.scan_source(world["store"], cfg, "e", "run-1")


def test_scan_artifact_registered_fresh(world):
    cfg = make_cfg(world)
    make_file(world["E"] / "a.mp3", b"a")
    scan.scan_source(world["store"], cfg, "e", "run-1")
    assert world["store"].artifact_fresh("scan:e", cfg.config_hash)
    art = world["store"].artifact_get("scan:e")
    assert art.scope == {"root": str(world["E"]), "name": "e"}


def test_rehash_under_refreshes_silent_content_change(world):
    """A file whose BYTES changed while size+mtime stayed identical (bit-rot /
    torn read) is invisible to the incremental fast-path forever; --rehash-under
    bypasses the fast-path for its prefix and refreshes the stored hash."""
    cfg = make_cfg(world)
    p = make_file(world["lib"] / "Docs" / "old.ppt", b"A" * 4096)
    st0 = os.stat(p)
    scan.scan_library(world["store"], cfg, "run-1")
    row = next(r for r in world["store"].index_iter()
               if r["relpath"].endswith("old.ppt"))
    old_hash = row["quick_hash"]

    # silent change: same size, mtime restored
    p.write_bytes(b"B" * 4096)
    os.utime(p, ns=(st0.st_atime_ns, st0.st_mtime_ns))

    n, _ = scan.scan_library(world["store"], cfg, "run-2")
    assert n == 0                                    # fast-path hides it
    n, _ = scan.scan_library(world["store"], cfg, "run-3",
                             rehash_under=["Docs"])
    assert n == 1                                    # forced re-hash caught it
    row = next(r for r in world["store"].index_iter()
               if r["relpath"].endswith("old.ppt"))
    assert row["quick_hash"] != old_hash
