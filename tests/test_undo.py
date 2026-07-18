"""P21/C1: mlo undo builds a reverse plan for a run's DONE placement ops and
applies it through the normal plan/apply path — no special-cased execution."""
from __future__ import annotations

import os

import pytest
from conftest import make_file
from helpers_plan import make_cfg
from mlo import fingerprint, undo as undomod
from mlo.apply import apply_plan
from mlo.plan import PlanError
from mlo.report import read_plan


def _moved(kernel, run_id, src, dst, content=b"hello world"):
    p = make_file(src, content)
    size, qh = fingerprint.quick(str(p))
    res = kernel(execute=True, run_id=run_id).move_within(str(src), str(dst),
                                                           size, qh)
    assert res.status == "done"
    return size, qh


def test_undo_reverses_a_move_within(world, kernel):
    cfg = make_cfg(world)
    store = world["store"]
    run_id = store.start_run("organize", [], cfg.config_hash, "t")
    src = world["lib"] / "A" / "song.mp3"
    dst = world["lib"] / "B" / "song.mp3"
    _moved(kernel, run_id, src, dst)
    assert not src.exists() and dst.exists()

    ures = undomod.build_undo(store, cfg, run_id)
    assert ures.n_rows == 1
    _, rows, _ = read_plan(ures.path)
    assert os.path.normcase(rows[0]["src"]) == os.path.normcase(str(dst))
    assert os.path.normcase(rows[0]["dst"]) == os.path.normcase(str(src))

    ares = apply_plan(store, cfg, ures.path,
                      store.start_run("apply", [], cfg.config_hash, "t"),
                      execute=True)
    assert ares.counts.get("done") == 1
    assert src.exists() and not dst.exists()


def test_undo_chains_multiple_moves_lifo(world, kernel):
    """A -> B -> C within one run undoes as C -> B, then B -> A."""
    cfg = make_cfg(world)
    store = world["store"]
    run_id = store.start_run("organize", [], cfg.config_hash, "t")
    a = world["lib"] / "A" / "f.mp3"
    b = world["lib"] / "B" / "f.mp3"
    c = world["lib"] / "C" / "f.mp3"
    _moved(kernel, run_id, a, b)
    size, qh = fingerprint.quick(str(b))
    res = kernel(execute=True, run_id=run_id).move_within(str(b), str(c), size, qh)
    assert res.status == "done"

    ures = undomod.build_undo(store, cfg, run_id)
    assert ures.n_rows == 2
    ares = apply_plan(store, cfg, ures.path,
                      store.start_run("apply", [], cfg.config_hash, "t"),
                      execute=True)
    assert ares.counts.get("done") == 2
    assert a.exists() and not b.exists() and not c.exists()


def test_undo_skips_copy_in_with_a_note(world, kernel):
    cfg = make_cfg(world)
    store = world["store"]
    run_id = store.start_run("sweep", [], cfg.config_hash, "t")
    src = world["E"] / "src" / "f.mp3"
    dst = world["lib"] / "f.mp3"
    p = make_file(src, b"hello")
    size, qh = fingerprint.quick(str(p))
    res = kernel(execute=True, run_id=run_id).copy_in(str(src), str(dst), size, qh)
    assert res.status == "done"

    ures = undomod.build_undo(store, cfg, run_id)
    assert ures.n_rows == 0
    assert any("copy_in" in n for n in ures.notes)


def test_undo_skips_a_row_whose_file_moved_since(world, kernel):
    cfg = make_cfg(world)
    store = world["store"]
    run_id = store.start_run("organize", [], cfg.config_hash, "t")
    src = world["lib"] / "A" / "f.mp3"
    dst = world["lib"] / "B" / "f.mp3"
    _moved(kernel, run_id, src, dst)
    # simulate further drift: the file moves again outside undo's knowledge
    dst2 = world["lib"] / "C" / "f.mp3"
    dst2.parent.mkdir(parents=True, exist_ok=True)
    dst.rename(dst2)

    ures = undomod.build_undo(store, cfg, run_id)
    assert ures.n_rows == 0
    assert any("no longer present" in n for n in ures.notes)


def test_undo_stage_move_reported_never_a_crashing_plan(world, kernel):
    """Super-review H3: reversal of a stage_move (staging -> origin) fits no
    kernel op kind — emitting it as move_within made apply raise an unhandled
    ValueError. It must be reported in the notes, not planned."""
    cfg = make_cfg(world)
    store = world["store"]
    run_id = store.start_run("dedup", [], cfg.config_hash, "t")
    src = make_file(world["E"] / "src" / "dup.mp3", b"D" * 64)
    size, qh = fingerprint.quick(str(src))
    dst = world["E"] / "Delete" / "dup.mp3"
    res = kernel(execute=True, run_id=run_id).stage_move(str(src), str(dst),
                                                          size, qh)
    assert res.status == "done"

    ures = undomod.build_undo(store, cfg, run_id)
    assert ures.n_rows == 0
    assert any("cannot be auto-reversed" in n for n in ures.notes)
    # the empty plan applies cleanly — no ValueError, no crash
    ares = apply_plan(store, cfg, ures.path,
                      store.start_run("apply", [], cfg.config_hash, "t"),
                      execute=True)
    assert ares.exit_code == 0
    assert dst.exists()      # the staged file is untouched


def test_undo_skips_replaced_content_at_post_run_location(world, kernel):
    """Super-review M5: if something REPLACED the moved file at its post-run
    path, undo must not relocate the impostor — the journaled pre-fingerprint
    is the arbiter."""
    cfg = make_cfg(world)
    store = world["store"]
    run_id = store.start_run("organize", [], cfg.config_hash, "t")
    src = world["lib"] / "A" / "song.mp3"
    dst = world["lib"] / "B" / "song.mp3"
    _moved(kernel, run_id, src, dst, content=b"ORIGINAL BYTES")
    dst.write_bytes(b"AN IMPOSTOR, DIFFERENT CONTENT")

    ures = undomod.build_undo(store, cfg, run_id)
    assert ures.n_rows == 0
    assert any("no longer matches the content" in n for n in ures.notes)


def test_undo_counts_disposed_ops_in_notes(world, kernel):
    cfg = make_cfg(world)
    store = world["store"]
    run_id = store.start_run("apply", [], cfg.config_hash, "t")
    f = make_file(world["E"] / "Delete" / "junk.bin")
    size, qh = fingerprint.quick(str(f))

    def fake_disposer(lp, dp):
        os.remove(lp)

    res = kernel(execute=True, run_id=run_id,
                 disposer=fake_disposer).dispose(str(f), size, qh)
    assert res.status == "done"

    ures = undomod.build_undo(store, cfg, run_id)
    assert ures.n_rows == 0
    assert any("Recycle Bin" in n for n in ures.notes)


def test_undo_unknown_run_id_raises(world):
    cfg = make_cfg(world)
    with pytest.raises(PlanError):
        undomod.build_undo(world["store"], cfg, "no-such-run")


def test_undo_nothing_to_undo_note(world):
    cfg = make_cfg(world)
    store = world["store"]
    run_id = store.start_run("scan", [], cfg.config_hash, "t")
    ures = undomod.build_undo(store, cfg, run_id)
    assert ures.n_rows == 0
    assert "nothing to undo" in ures.notes
