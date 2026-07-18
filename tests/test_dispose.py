"""P21/C2 — the L18 amendment: build_dispose only includes files the journal
recognizes as this engine's own staged output; unjournaled content and
.mlopart residue are excluded and reported, never disposed blind. Protected
content that somehow ended up in staging still hard-refuses the WHOLE build
via _register_plan's existing L12 check (_reject_protected) — the same
contract every other builder already has."""
from __future__ import annotations

import os

import pytest
from conftest import make_file
from helpers_plan import make_cfg
from mlo import fingerprint, plan as planmod
from mlo.apply import DisposeNotConfirmed, apply_plan, reconcile_pending
from mlo.plan import PlanError
from mlo.report import read_plan


def _stage_a_file(world, cfg, kernel, rel="song.mp3", content=b"hello"):
    """A real journaled stage_move — store.staged_dsts() must recognize the
    result, since that's the precondition build_dispose relies on."""
    src = make_file(world["E"] / "src" / rel, content)
    size, qh = fingerprint.quick(str(src))
    dst = os.path.join(cfg.staging["E"], rel)
    run_id = world["store"].start_run("stage", [], cfg.config_hash, "t")
    res = kernel(execute=True, run_id=run_id).stage_move(str(src), dst, size, qh)
    assert res.status == "done"
    return dst


def test_build_dispose_includes_journaled_staged_files(world, kernel):
    cfg = make_cfg(world)
    dst = _stage_a_file(world, cfg, kernel)
    res = planmod.build_dispose(world["store"], cfg)
    assert res.n_rows == 1
    _, rows, _ = read_plan(res.path)
    assert rows[0]["src"] == dst == rows[0]["dst"]
    assert rows[0]["kind"] == "dispose"


def test_build_dispose_excludes_unjournaled_staging_content(world, kernel):
    cfg = make_cfg(world)
    make_file(world["E"] / "Delete" / "mystery.bin")   # dropped in by hand
    res = planmod.build_dispose(world["store"], cfg)
    assert res.n_rows == 0
    assert any("journal can't explain" in n for n in res.notes)


def test_build_dispose_excludes_mlopart_residue(world):
    cfg = make_cfg(world)
    make_file(world["E"] / "Delete" / "half-copy.bin.mlopart")
    res = planmod.build_dispose(world["store"], cfg)
    assert res.n_rows == 0


def test_build_dispose_unknown_staging_key_raises(world):
    cfg = make_cfg(world)
    with pytest.raises(PlanError, match="unknown staging key"):
        planmod.build_dispose(world["store"], cfg, staging_key="nope")


def test_build_dispose_scoped_to_one_staging_key(world, kernel):
    cfg = make_cfg(world)
    _stage_a_file(world, cfg, kernel, rel="a.mp3")

    src_i = make_file(world["I"] / "srcdir" / "b.mp3")
    size, qh = fingerprint.quick(str(src_i))
    dst_i = os.path.join(cfg.staging["I"], "b.mp3")
    run_id = world["store"].start_run("stage", [], cfg.config_hash, "t")
    res = kernel(execute=True, run_id=run_id).stage_move(str(src_i), dst_i, size, qh)
    assert res.status == "done"

    only_e = planmod.build_dispose(world["store"], cfg, staging_key="E")
    assert only_e.n_rows == 1
    _, rows, _ = read_plan(only_e.path)
    assert rows[0]["src"] == os.path.join(cfg.staging["E"], "a.mp3")


def test_build_dispose_robust_to_redundant_separators_in_staging_root(world, kernel):
    """Regression: a staging root string with doubled/redundant separators
    (the shape a TOML-literal string can produce, e.g. `mlo dispose` on a
    real mlo.toml) must not make a genuinely-journaled file look
    unjournaled — os.path.normpath must run on both sides of the compare."""
    import dataclasses

    cfg = make_cfg(world)
    dst = _stage_a_file(world, cfg, kernel)
    doubled = cfg.staging["E"].replace(os.sep, os.sep * 2)
    cfg2 = dataclasses.replace(cfg, staging={**cfg.staging, "E": doubled})
    res = planmod.build_dispose(world["store"], cfg2, staging_key="E")
    assert res.n_rows == 1
    _, rows, _ = read_plan(res.path)
    assert os.path.normpath(rows[0]["src"]) == os.path.normpath(dst)


def test_build_dispose_empty_staging_is_zero_rows(world):
    cfg = make_cfg(world)
    res = planmod.build_dispose(world["store"], cfg)
    assert res.n_rows == 0
    assert "files to dispose: 0" in res.notes[0]


def test_build_dispose_refuses_whole_plan_when_protected_content_staged(world, kernel):
    """A journaled staged file whose path is protected is an anomaly (it
    could only have arrived outside the engine's own stage_move, which
    already policy-checks placement) — build_dispose refuses the whole
    plan rather than silently skipping it, same as every other builder."""
    cfg = make_cfg(world)
    _stage_a_file(world, cfg, kernel, rel="ok.mp3")

    src = make_file(world["E"] / "src" / "bluestacks-ish" / "x.bin")
    size, qh = fingerprint.quick(str(src))
    dst = os.path.join(cfg.staging["E"], "bluestacks-ish", "x.bin")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(str(src), dst)
    world["store"].journal_intent("anomaly-run", None, "fake-op-id",
                                  "stage_move", str(src), dst, size, qh)
    world["store"].complete_op("fake-op-id", "done")

    with pytest.raises(PlanError, match="protected"):
        planmod.build_dispose(world["store"], cfg)


# ── apply_plan's dispose-confirmation gate (typed row-count, P21/C2) ────────

def _fake_disposer(calls: list):
    """Records the call AND removes the file — simulating what the real
    disposer does (the file leaves its original location), so apply_plan's
    post-condition audit tail sees a genuine success. Test-only: the
    no-deletion AST law (test_architecture.py) scopes to src/mlo, not
    tests/ — this isn't the production disposer."""
    def disposer(lpath: str, display_path: str) -> None:
        calls.append((lpath, display_path))
        os.remove(lpath)
    return disposer


def test_apply_dispose_without_confirmation_refuses(world, kernel):
    cfg = make_cfg(world)
    _stage_a_file(world, cfg, kernel)
    res = planmod.build_dispose(world["store"], cfg)
    run_id = world["store"].start_run("apply", [], cfg.config_hash, "t")
    with pytest.raises(DisposeNotConfirmed):
        apply_plan(world["store"], cfg, res.path, run_id, execute=True,
                  drive_of=world["drive_of"])


def test_apply_dispose_with_wrong_count_refuses(world, kernel):
    cfg = make_cfg(world)
    _stage_a_file(world, cfg, kernel)
    res = planmod.build_dispose(world["store"], cfg)
    run_id = world["store"].start_run("apply", [], cfg.config_hash, "t")
    with pytest.raises(DisposeNotConfirmed):
        apply_plan(world["store"], cfg, res.path, run_id, execute=True,
                  drive_of=world["drive_of"], confirm_dispose=res.n_rows + 1)


def test_apply_dispose_rehearse_needs_no_confirmation(world, kernel):
    cfg = make_cfg(world)
    _stage_a_file(world, cfg, kernel)
    res = planmod.build_dispose(world["store"], cfg)
    run_id = world["store"].start_run("apply", [], cfg.config_hash, "t")
    ares = apply_plan(world["store"], cfg, res.path, run_id, execute=False,
                      drive_of=world["drive_of"])
    assert ares.executed is False


def test_apply_dispose_with_correct_confirmation_executes(world, kernel):
    cfg = make_cfg(world)
    dst = _stage_a_file(world, cfg, kernel)
    res = planmod.build_dispose(world["store"], cfg)
    assert res.n_rows == 1
    calls: list = []
    run_id = world["store"].start_run("apply", [], cfg.config_hash, "t")
    ares = apply_plan(world["store"], cfg, res.path, run_id, execute=True,
                      drive_of=world["drive_of"],
                      confirm_dispose=res.n_rows, disposer=_fake_disposer(calls))
    assert ares.counts.get("done") == 1
    assert len(calls) == 1 and calls[0][1] == dst
    assert ares.exit_code == 0


def test_dispose_residual_still_requires_confirmation(world, kernel):
    """Super-review H2: a partially-failed dispose run emits a
    'dispose-residual' plan whose rows still carry kind='dispose' and the
    real disposer — the C68 typed-confirmation gate must cover it too, and
    the suggested next command must carry --confirm-dispose."""
    cfg = make_cfg(world)
    dst_a = _stage_a_file(world, cfg, kernel, rel="a.mp3", content=b"AAA")
    _stage_a_file(world, cfg, kernel, rel="b.mp3", content=b"BBB")
    res = planmod.build_dispose(world["store"], cfg)
    assert res.n_rows == 2

    os.remove(dst_a)   # one row drifts (source missing) -> residual
    calls: list = []
    run_id = world["store"].start_run("apply", [], cfg.config_hash, "t")
    ares = apply_plan(world["store"], cfg, res.path, run_id, execute=True,
                      drive_of=world["drive_of"], confirm_dispose=2,
                      disposer=_fake_disposer(calls))
    assert ares.residual_plan is not None
    from mlo.report import read_plan as _rp
    header, rrows, _ = _rp(ares.residual_plan)
    assert header["kind"] == "dispose-residual"
    assert all(r["kind"] == "dispose" for r in rrows)
    # the suggested command must carry the typed confirmation
    import json as _json
    summary = _json.load(open(ares.summary_path, encoding="utf-8"))
    resid_cmd = next(s["cmd"] for s in summary["suggested_next"]
                     if "apply" in s["cmd"])
    assert f"--confirm-dispose {len(rrows)}" in resid_cmd

    # the gate itself: bare --execute on the residual refuses
    run_id2 = world["store"].start_run("apply", [], cfg.config_hash, "t")
    with pytest.raises(DisposeNotConfirmed):
        apply_plan(world["store"], cfg, ares.residual_plan, run_id2,
                   execute=True, drive_of=world["drive_of"],
                   disposer=_fake_disposer([]))


def test_build_dispose_excludes_replaced_content_at_journaled_path(world, kernel):
    """Super-review M19: a journaled staging PATH whose bytes were replaced
    since the engine staged it must not be disposed blind — the plan compares
    the current fingerprint against the journaled one."""
    cfg = make_cfg(world)
    dst = _stage_a_file(world, cfg, kernel, rel="swap.mp3", content=b"ORIGINAL")
    with open(dst, "wb") as fh:
        fh.write(b"REPLACED CONTENT, DIFFERENT BYTES")
    res = planmod.build_dispose(world["store"], cfg)
    assert res.n_rows == 0
    assert any("no longer match the content" in n for n in res.notes)


# ── crash recovery for dispose (single-path, like rmdir_empty, P21/C6) ─────

def test_reconcile_pending_dispose_file_gone_is_done(world, kernel):
    cfg = make_cfg(world)
    dst = _stage_a_file(world, cfg, kernel)
    size, qh = fingerprint.quick(dst)
    op_id = "fake-dispose-op"
    world["store"].journal_intent("crashed", None, op_id, "dispose", dst, dst,
                                  size, qh)
    os.remove(dst)   # simulate: the OS actually disposed it before the crash
    n = reconcile_pending(world["store"], cfg.library_root)
    assert n == 1
    assert world["store"].op_state(op_id) == "done"


def test_reconcile_pending_dispose_file_present_is_retryable(world, kernel):
    cfg = make_cfg(world)
    dst = _stage_a_file(world, cfg, kernel)
    size, qh = fingerprint.quick(dst)
    op_id = "fake-dispose-op-2"
    world["store"].journal_intent("crashed", None, op_id, "dispose", dst, dst,
                                  size, qh)
    n = reconcile_pending(world["store"], cfg.library_root)
    assert n == 1
    assert world["store"].op_state(op_id) == "failed"
