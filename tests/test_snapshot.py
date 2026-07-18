"""E1: the library-state snapshot surfaces media sitting in a generic bin (the
'current shit state' the loop targets)."""
from __future__ import annotations

import os

from conftest import make_file
from helpers import make_cfg
from mlo import fingerprint, snapshot

TAX = {"Video": (".mp4", ".mkv"), "Audio": (".mp3",), "Photos": (".jpg",),
       "Documents": (".pdf",)}


def _seed(world, rels):
    st = world["store"]
    for rel in rels:
        p = make_file(world["lib"] / rel, rel.encode() * 5)
        size, qh = fingerprint.quick(str(p))
        st.index_upsert(rel.replace("/", os.sep), size, qh,
                        os.stat(p).st_mtime_ns, "s")
    st.index_commit()


def test_snapshot_flags_media_in_a_generic_bin(world):
    cfg = make_cfg(world, taxonomy=TAX)
    _seed(world, ["Other/Unsorted/a.mp4", "Other/Unsorted/b.mp4",
                  "Other/Unsorted/c.mkv",
                  "Video/Movies/Tamil/Roja (1992)/Roja (1992).mkv"])
    snap = snapshot.build_snapshot(world["store"], cfg)
    by_folder = {f["folder"]: f for f in snap["folders"]}

    other = by_folder[os.path.join("Other", "Unsorted")]
    assert other["dominant_bucket"] == "Video"
    assert other["suspected_home"] == "Video"     # video in a non-Video area
    assert other["problem"] is True
    assert other["confidence"] == 1.0

    movie = by_folder[os.path.join("Video", "Movies")]
    assert movie["problem"] is False              # correctly placed: not flagged
    assert snap["problem_count"] >= 1


def test_snapshot_scopes_under_prefix(world):
    cfg = make_cfg(world, taxonomy=TAX)
    _seed(world, ["Other/Unsorted/a.mp4", "Video/Movies/x/y.mkv"])
    snap = snapshot.build_snapshot(world["store"], cfg, under="Other")
    folders = {f["folder"] for f in snap["folders"]}
    assert folders == {os.path.join("Other", "Unsorted")}
    assert snap["total_files"] == 1
