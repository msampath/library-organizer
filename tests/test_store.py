"""Store: journal semantics, transactional index effects, freshness (L1, L7, L16)."""
from __future__ import annotations

import os

from mlo.store import Store


def test_journal_roundtrip(world):
    st: Store = world["store"]
    run = st.start_run("test", [], "cfg-hash", "0.0")
    assert st.journal_pos() == 0
    st.journal_intent(run, None, "op-1", "copy_in", "/a", "/b", 5, "h")
    assert st.op_state("op-1") == "pending"
    st.complete_op("op-1", "done")
    assert st.op_state("op-1") == "done"
    assert st.journal_pos() == 1
    st.finish_run(run, "completed")
    assert st.get_run(run)["status"] == "completed"


def test_pending_reconciliation_listing(world):
    st: Store = world["store"]
    run = st.start_run("test", [], "h", "0.0")
    st.journal_intent(run, "plan-x", "op-p", "stage_move", "/s", "/d", 1, "q")
    pend = st.pending_ops()
    assert len(pend) == 1 and pend[0]["op_id"] == "op-p"
    st.complete_op("op-p", "done")
    assert st.pending_ops() == []


def test_index_effect_applied_atomically_with_done(world):
    st: Store = world["store"]
    run = st.start_run("test", [], "h", "0.0")
    st.journal_intent(run, None, "op-i", "copy_in", "/src/x.mp3",
                      os.path.join(str(world["lib"]), "Audio", "x.mp3"), 9, "qh")
    st.complete_op("op-i", "done",
                   index_effect=("insert", os.path.join("Audio", "x.mp3"),
                                 9, "qh", 123, "scan-1"))
    got = st.index_get(os.path.join("Audio", "x.mp3"))
    assert got == {"size": 9, "quick_hash": "qh", "mtime_ns": 123}
    assert st.index_lookup(9, "qh")


def test_scan_artifact_flips_stale_when_op_touches_scope(world):
    st: Store = world["store"]
    run = st.start_run("test", [], "cfg", "0.0")
    src_root = str(world["E"])
    st.artifact_register("scan:E", "scan", {"root": src_root}, "cfg", run)
    assert st.artifact_fresh("scan:E", "cfg")
    st.journal_intent(run, None, "op-s", "stage_move",
                      os.path.join(src_root, "f.bin"),
                      os.path.join(src_root, "Delete", "f.bin"), 1, "q")
    st.complete_op("op-s", "done")
    assert not st.artifact_fresh("scan:E", "cfg")
    assert st.artifact_get("scan:E").status == "stale"


def test_index_artifact_never_flips(world):
    st: Store = world["store"]
    run = st.start_run("test", [], "cfg", "0.0")
    lib = str(world["lib"])
    st.artifact_register("index:library", "index", {"root": lib}, "cfg", run)
    st.journal_intent(run, None, "op-l", "copy_in", "/outside/a.bin",
                      os.path.join(lib, "Audio", "a.bin"), 2, "q2")
    st.complete_op("op-l", "done",
                   index_effect=("insert", os.path.join("Audio", "a.bin"),
                                 2, "q2", 0, "s"))
    assert st.artifact_fresh("index:library", "cfg")   # maintained, not stale


def test_artifact_staleness_on_config_change(world):
    st: Store = world["store"]
    run = st.start_run("test", [], "cfg-A", "0.0")
    st.artifact_register("verdicts:E", "verdicts", {"root": str(world["E"])},
                         "cfg-A", run)
    assert st.artifact_fresh("verdicts:E", "cfg-A")
    assert not st.artifact_fresh("verdicts:E", "cfg-B")  # config changed


def test_source_files_verdict_lifecycle(world):
    st: Store = world["store"]
    st.source_upsert("E", "a/b.mp3", 10, "q", 0, "scan-1")
    st.source_commit()
    st.source_set_verdict("E", "a/b.mp3", "UNIQUE", "not-in-index")
    st.source_commit()
    rows = list(st.source_iter("E", "UNIQUE"))
    assert len(rows) == 1 and rows[0]["verdict_rule"] == "not-in-index"
    assert st.source_verdict_counts("E") == {"UNIQUE": 1}
    # re-scan resets the verdict (stale verdicts cannot leak across scans)
    st.source_upsert("E", "a/b.mp3", 11, "q2", 0, "scan-2")
    st.source_commit()
    rows = list(st.source_iter("E"))
    assert rows[0]["verdict"] is None


def test_two_connections_share_a_workspace(world):
    """WAL + busy_timeout: a second process opening the same workspace can read
    while the first writes, and a brief write overlap waits rather than raising
    'database is locked' (found when a live eval collided with a running scan)."""
    st: Store = world["store"]
    st.index_upsert("a/x.mp3", 1, "q", 0, "s")
    st.index_commit()
    other = Store.open(st.workspace)
    try:
        assert other.index_count() == 1                 # concurrent reader sees it
        run = other.start_run("t2", [], "h", "0.0")      # concurrent writer waits, ok
        other.finish_run(run, "completed")
    finally:
        other.close()


def test_surrogate_paths_roundtrip_via_blobs(world):
    st: Store = world["store"]
    weird = "dir/br\udc80ken.mp4"      # lone surrogate — TEXT would raise
    st.index_upsert(weird, 7, "qq", 0, "s")
    st.index_commit()
    assert st.index_get(weird) is not None
    assert any(r["relpath"] == weird for r in st.index_iter())
