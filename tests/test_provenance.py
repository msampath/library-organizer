"""provenance — journal-derived origin tracing, the personal-source signal, and
honest coverage boundaries. INFORMS only (this module never mutates)."""
from __future__ import annotations

import os

from helpers import make_cfg
from mlo import provenance


def test_origin_trace_and_personal_signal(world):
    """A WhatsApp video copied into the library traces back to its source path,
    and that path's folder names yield a 'personal' hint. A file the external
    pipeline placed has no journal record — reported as an honest boundary."""
    cfg = make_cfg(world)
    st = world["store"]
    lib = str(world["lib"])
    rel = os.path.join("Video", "Personal", "G", "clip.mp4")
    dst = os.path.join(lib, rel)
    src = os.path.join(str(world["E"]), "phone", "WhatsApp", "Media", "clip.mp4")

    run = st.start_run("organize", [], cfg.config_hash, "t")
    st.journal_intent(run, "plan", "op-clip", "copy_in", src, dst, 100, "qh")
    st.complete_op("op-clip", "done",
                   index_effect=("insert", rel, 100, "qh", 0, "scan"))
    # a file placed by the external v5 pipeline — indexed, but no journal op
    st.index_upsert(os.path.join("Video", "Movies", "X", "x.mp4"),
                    200, "qh2", 0, "scan")
    st.index_commit()

    omap = provenance.build_origin_map(st)
    got = provenance.origin_of(cfg, omap, rel)
    assert got == src
    assert provenance.origin_signal(got) == "personal"        # INFORMS -> hint

    # the externally-placed file: origin is honestly unknown, not guessed
    assert provenance.origin_of(
        cfg, omap, os.path.join("Video", "Movies", "X", "x.mp4")) is None
    cov = provenance.coverage(st, cfg, omap)
    assert cov["total"] == 2 and cov["traced"] == 1 and cov["untraced"] == 1
    assert cov["pct"] == 50.0


def test_origin_traces_through_internal_moves(world):
    """A file copied in, then reorganized (move_within), still traces to its
    original external source — the chain is followed to the boundary."""
    cfg = make_cfg(world)
    st = world["store"]
    lib = str(world["lib"])
    src_ext = os.path.join(str(world["E"]), "films", "a.mkv")
    mid_rel = os.path.join("Video", "eSrc", "a.mkv")
    mid = os.path.join(lib, mid_rel)
    final_rel = os.path.join("Video", "Movies", "Other", "A (2010)", "A (2010).mkv")
    final = os.path.join(lib, final_rel)

    run = st.start_run("organize", [], cfg.config_hash, "t")
    st.journal_intent(run, "p", "op1", "copy_in", src_ext, mid, 10, "h")
    st.complete_op("op1", "done", index_effect=("insert", mid_rel, 10, "h", 0, "s"))
    st.journal_intent(run, "p", "op2", "move_within", mid, final, 10, "h")
    st.complete_op("op2", "done", index_effect=("move", mid_rel, final_rel))

    omap = provenance.build_origin_map(st)
    assert provenance.origin_of(cfg, omap, final_rel) == src_ext


def test_origin_signal_is_none_for_non_personal_and_empty():
    assert provenance.origin_signal(None) is None
    assert provenance.origin_signal("") is None
    assert provenance.origin_signal(
        os.path.join("E:", "Movies", "Hollywood", "Inception.mkv")) is None
