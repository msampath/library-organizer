"""Verify: external edits (L14), copy residue (L15), protected staging (L12)."""
from __future__ import annotations

import os

from conftest import make_file
from helpers_plan import make_cfg
from mlo import fingerprint
from mlo.verify import verify_library, verify_staging


def indexed_file(world, rel: str, content: bytes):
    p = make_file(world["lib"] / rel, content)
    size, qh = fingerprint.quick(str(p))
    world["store"].index_upsert(rel.replace("/", os.sep), size, qh,
                                os.stat(p).st_mtime_ns, "scan-v")
    world["store"].index_commit()
    return p


def test_clean_library_is_clean(world):
    cfg = make_cfg(world)
    indexed_file(world, "Video/a.mp4", b"A" * 100)
    f = verify_library(world["store"], cfg)
    assert f.counts() == {k: 0 for k in f.counts()}


def test_quick_scan_detects_external_moves(world):
    cfg = make_cfg(world)
    p = indexed_file(world, "Video/b.mp4", b"B" * 100)
    stray = make_file(world["lib"] / "Video" / "stray.mp4", b"S")   # never indexed
    os.rename(p, world["lib"] / "Video" / "renamed.mp4")            # external move
    f = verify_library(world["store"], cfg)
    assert os.path.join("Video", "b.mp4") in f.missing
    assert os.path.join("Video", "renamed.mp4") in f.unindexed
    assert os.path.join("Video", "stray.mp4") in f.unindexed
    assert not f.blocking


def test_quick_detects_content_drift(world):
    cfg = make_cfg(world)
    p = indexed_file(world, "Docs/x.txt", b"original")
    p.write_bytes(b"externally edited, longer than before")
    f = verify_library(world["store"], cfg)
    assert os.path.join("Docs", "x.txt") in f.drifted


def test_deep_catches_same_size_content_change(world):
    """A same-size, mtime-restored edit is invisible to the stat fast-path but
    caught by --deep re-fingerprinting (the stale-hash direction of L13)."""
    cfg = make_cfg(world)
    p = indexed_file(world, "Docs/y.txt", b"AAAAAAAA")
    st = os.stat(p)
    p.write_bytes(b"BBBBBBBB")                      # same length, different bytes
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))  # restore mtime
    rel = os.path.join("Docs", "y.txt")
    assert rel not in verify_library(world["store"], cfg, quick=True).drifted
    assert rel in verify_library(world["store"], cfg, quick=False).drifted


def test_missing_candidate_confirmed_present_on_disk_is_not_reported(world):
    """C49: an index row whose rel string the walk never matches (short-name/
    case/normalization variant) must not be reported missing when the file it
    actually names is still on disk. Simulated here with a case-variant index
    row for the same on-disk file -- os.path.exists resolves it (NTFS is
    case-insensitive), exactly like the short-name aliasing seen live."""
    cfg = make_cfg(world)
    p = indexed_file(world, "Video/b.mp4", b"B" * 100)   # real row, walk matches it
    size, qh = fingerprint.quick(str(p))
    world["store"].index_upsert(os.path.join("Video", "B.MP4"), size, qh,
                                 os.stat(p).st_mtime_ns, "scan-v")
    world["store"].index_commit()
    f = verify_library(world["store"], cfg)
    assert os.path.join("Video", "B.MP4") not in f.missing


def test_missing_candidate_confirmed_absent_is_still_reported(world):
    """C49: a genuinely deleted file must still be reported missing."""
    cfg = make_cfg(world)
    p = indexed_file(world, "Video/deleted.mp4", b"D" * 100)
    os.remove(p)
    f = verify_library(world["store"], cfg)
    assert os.path.join("Video", "deleted.mp4") in f.missing


def test_mlopart_residue_flagged(world):
    cfg = make_cfg(world)
    make_file(world["lib"] / "Backups" / "big.bin.mlopart", b"partial")
    f = verify_library(world["store"], cfg)
    assert any(x.endswith(".mlopart") for x in f.mlopart)


def test_protected_content_in_staging_blocks(world):
    cfg = make_cfg(world)
    make_file(world["E"] / "Delete" / "Bluestacks Backup" / "disk.img", b"VM")
    f = verify_staging(world["store"], cfg)
    assert f.blocking
    assert any("bluestacks" in p.lower() for p in f.protected_in_staging)


def test_unjournaled_staging_content_flagged(world, kernel):
    cfg = make_cfg(world)
    src = make_file(world["E"] / "s" / "dup.bin", b"D" * 40)
    size, qh = fingerprint.quick(str(src))
    dst = world["E"] / "Delete" / "s" / "dup.bin"
    k = kernel(execute=True)
    assert k.stage_move(str(src), str(dst), size, qh).status == "done"
    make_file(world["E"] / "Delete" / "manual-drop.bin", b"who put this here")
    f = verify_staging(world["store"], cfg)
    flagged = [os.path.basename(p) for p in f.unjournaled_staging]
    assert "manual-drop.bin" in flagged
    assert "dup.bin" not in flagged                  # journal explains it
