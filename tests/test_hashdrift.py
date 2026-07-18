"""Hash-drift (S5): after a (simulated) metadata write the library index is
re-fingerprinted so a later dedup sees current content — index-only, no file
mutation."""
from __future__ import annotations

import os

from conftest import make_file
from helpers import make_cfg
from mlo import fingerprint, hashdrift


def test_recompute_updates_index_after_a_write(world):
    cfg = make_cfg(world, taxonomy={"Audio": (".mp3",)})
    st = world["store"]
    rel = os.path.join("Audio", "Music", "song.mp3")
    p = make_file(world["lib"] / rel, b"ORIGINAL" * 100)
    size, qh = fingerprint.quick(str(p))
    st.index_upsert(rel, size, qh, os.stat(p).st_mtime_ns, "s")
    st.index_commit()

    # simulate an enrichment write-back changing the file's bytes
    p.write_bytes(b"ORIGINAL" * 100 + b"ID3-TAG-EMBEDDED")
    res = hashdrift.recompute(st, cfg, rel)
    assert res is not None
    old, new = res
    assert old == qh and new != qh
    assert st.index_get(rel)["quick_hash"] == new
    # dedup keyed on (size, quick_hash) now finds it under the NEW fingerprint
    nsize, nqh = fingerprint.quick(str(p))
    assert rel in st.index_lookup(nsize, nqh)


def test_recompute_is_none_for_unindexed(world):
    cfg = make_cfg(world, taxonomy={"Audio": (".mp3",)})
    assert hashdrift.recompute(world["store"], cfg, "not/indexed.mp3") is None
