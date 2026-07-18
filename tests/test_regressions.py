"""Regression tests for the defects the multi-agent review reproduced (C1-C6).
Each would fail against the pre-fix code; together they are the proof that the
audit-tail / crash / freshness guarantees actually hold.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from helpers_plan import make_cfg, seed_source, seed_store
from mlo import config as cfgmod, plan as planmod
from mlo.apply import apply_plan
from mlo.config import ConfigError
from mlo.safeops import SafeOps
from mlo.store import Store


def _organize_plan(world, files, verdicts):
    cfg = make_cfg(world)
    pre = seed_source(world, cfg, files)
    seed_store(world, cfg, pre, verdicts)
    return cfg, planmod.build_organize(world["store"], cfg, "eSrc",
                                       drive_of=world["drive_of"])


# ── C1: a residual plan actually retries; "complete but wrong" is unreachable ──

def test_residual_retries_after_drift_is_fixed(world):
    files = {f"v/f{i}.mp4": bytes([65 + i]) * 400 for i in range(3)}
    cfg, plan = _organize_plan(
        world, files, {r: ("UNIQUE", "bucket:Video") for r in files})
    st = world["store"]
    from mlo.report import read_plan
    _, rows, _ = read_plan(plan.path)

    planned = Path(rows[1]["src"]).read_bytes()
    Path(rows[1]["src"]).write_bytes(b"transiently different")   # induce drift
    r1 = apply_plan(st, cfg, plan.path, st.start_run("a", [], cfg.config_hash, "t"),
                    execute=True, drive_of=world["drive_of"])
    assert r1.exit_code == 3 and r1.counts.get("skipped_drift") == 1

    Path(rows[1]["src"]).write_bytes(planned)                    # fix the cause
    r2 = apply_plan(st, cfg, r1.residual_plan,
                    st.start_run("b", [], cfg.config_hash, "t"),
                    execute=True, drive_of=world["drive_of"])
    # THE BUG (pre-fix): the drift op was terminal -> skipped_done -> exit 0 with
    # the file never written. Now it retries and completes.
    assert r2.counts.get("done") == 1 and r2.exit_code == 0
    assert Path(rows[1]["dst"]).exists()


def test_audit_failure_is_not_silently_complete(world, monkeypatch):
    files = {"v/only.mp4": b"Z" * 500}
    cfg, plan = _organize_plan(world, files, {"v/only.mp4": ("UNIQUE", "bucket:Video")})
    st = world["store"]

    def liar(self, kind, src, dst, pre_size, pre_qh):
        return None                                # journals done, does nothing

    monkeypatch.setattr(SafeOps, "_syscall", liar)
    r = apply_plan(st, cfg, plan.path, st.start_run("x", [], cfg.config_hash, "t"),
                   execute=True, drive_of=world["drive_of"])
    assert r.exit_code == 3 and r.residual_plan            # not a false success

    monkeypatch.undo()
    r2 = apply_plan(st, cfg, r.residual_plan,
                    st.start_run("y", [], cfg.config_hash, "t"),
                    execute=True, drive_of=world["drive_of"])
    # the reset (mark_retryable) let the residual actually do the work
    assert r2.counts.get("done") == 1
    assert Path(next(iter(files)) and
                os.path.join(cfg.library_root, "Video", "eSrc", "v", "only.mp4")).exists()


# ── C2: the intent row is durable before the act (survives a fresh connection) ─

def test_journal_intent_is_committed_before_act(world):
    st: Store = world["store"]
    run = st.start_run("t", [], "h", "0.0")
    st.journal_intent(run, "p", "op-durable", "copy_in", "/a", "/b", 1, "q")
    # a *separate* connection = what a crashed process's successor sees. Pre-fix
    # the intent wasn't committed, so this saw nothing and reconcile was blind.
    other = Store.open(st.workspace)
    try:
        assert other.op_state("op-durable") == "pending"
    finally:
        other.close()


# ── C3: rebuilding an executed plan does not revert it to fresh ───────────────

def test_rebuild_of_executed_plan_keeps_executed(world):
    files = {"v/a.mp4": b"A" * 300}
    cfg, plan = _organize_plan(world, files, {"v/a.mp4": ("UNIQUE", "bucket:Video")})
    st = world["store"]
    apply_plan(st, cfg, plan.path, st.start_run("a", [], cfg.config_hash, "t"),
               execute=True, drive_of=world["drive_of"])
    assert st.artifact_get(f"plan:{plan.plan_id}").status == "executed"
    # identical rebuild (same plan_id) must NOT flip it back to fresh, or the
    # dedup ordering gate would falsely re-block (L13).
    again = planmod.build_organize(st, cfg, "eSrc", drive_of=world["drive_of"])
    assert again.plan_id == plan.plan_id
    assert st.artifact_get(f"plan:{plan.plan_id}").status == "executed"
    # and dedup is therefore allowed
    ded = planmod.build_dedup(st, cfg, "eSrc", drive_of=world["drive_of"])
    assert ded.kind == "dedup"


# ── C4: staging inside the library is refused ────────────────────────────────

def test_staging_inside_library_refused(tmp_path):
    lib = tmp_path / "lib"
    (lib / "Delete").mkdir(parents=True)
    from mlo import winpath
    drive = winpath.drive_of(str(lib)) or "Z"    # real drive so the letter check passes
    cfg = cfgmod.load(_write(tmp_path, f'''
[library]
root = {str(lib)!r}
[staging]
{drive} = {str(lib / "Delete")!r}
'''))
    with pytest.raises(ConfigError, match="must not live inside the library"):
        cfgmod.validate(cfg, str(tmp_path / "ws"))


# ── C5: protected content cannot masquerade as a swept source ────────────────

def test_plan_build_refuses_protected_rows(world):
    files = {"BlueStacks/vm.img": b"V" * 200, "ok/keep.mp4": b"K" * 200}
    cfg = make_cfg(world)
    pre = seed_source(world, cfg, files)
    seed_store(world, cfg, pre, {
        "BlueStacks/vm.img": ("JUNK", "junk:x"),
        "ok/keep.mp4": ("UNIQUE", "bucket:Video")})
    # organize covers the unique; then dedup would include the protected JUNK row
    org = planmod.build_organize(world["store"], cfg, "eSrc",
                                 drive_of=world["drive_of"])
    world["store"].artifact_set_status(f"plan:{org.plan_id}", "executed")
    with pytest.raises(planmod.PlanError, match="protected"):
        planmod.build_dedup(world["store"], cfg, "eSrc", waive_organize=True,
                            drive_of=world["drive_of"])


# ── C6: an interrupted scan leaves its artifact non-fresh ────────────────────

def test_interrupted_scan_leaves_artifact_building(world, monkeypatch):
    from mlo import scan
    cfg = make_cfg(world)
    src = world["E"] / "src"
    for i in range(3):
        (src / f"f{i}.mp4").parent.mkdir(parents=True, exist_ok=True)
        (src / f"f{i}.mp4").write_bytes(bytes([i]) * 50)

    real = scan.fingerprint.quick
    calls = {"n": 0}

    def die_after_one(path):
        calls["n"] += 1
        if calls["n"] > 1:
            raise KeyboardInterrupt
        return real(path)

    monkeypatch.setattr(scan.fingerprint, "quick", die_after_one)
    with pytest.raises(KeyboardInterrupt):
        scan.scan_source(world["store"], cfg, "eSrc", "run-x")
    monkeypatch.undo()
    # THE BUG (pre-fix): artifact was only stamped at the end, or stamped fresh;
    # an interrupted scan must not present as fresh truth (L7).
    assert not world["store"].artifact_fresh("scan:eSrc", cfg.config_hash)
    assert world["store"].artifact_get("scan:eSrc").status == "building"


# ── C7: re-apply after an external library deletion is a clean no-op, not a
#      state-corrupting re-audit (2nd-order review, Finding 1) ───────────────

def test_reapply_after_external_delete_does_not_corrupt(world):
    files = {"v/keep.mp4": b"K" * 400}
    cfg, plan = _organize_plan(world, files, {"v/keep.mp4": ("UNIQUE", "bucket:Video")})
    st = world["store"]
    from mlo.report import read_plan
    _, rows, _ = read_plan(plan.path)
    op_id = rows[0]["op_id"]

    apply_plan(st, cfg, plan.path, st.start_run("a", [], cfg.config_hash, "t"),
               execute=True, drive_of=world["drive_of"])
    assert st.op_state(op_id) == "done"
    os.remove(rows[0]["dst"])                       # external deletion after the copy

    r = apply_plan(st, cfg, plan.path, st.start_run("b", [], cfg.config_hash, "t"),
                   execute=True, drive_of=world["drive_of"])
    # THE BUG (pre-fix): the audit re-ran on this prior-run op, reset it to
    # failed, and left a phantom index row. Now re-apply is a pure no-op.
    assert r.counts == {"skipped_done": 1} and r.exit_code == 0
    assert st.op_state(op_id) == "done"
    # the external deletion is verify's finding, not apply's to silently "fix"
    from mlo.verify import verify_library
    rel = os.path.relpath(rows[0]["dst"], cfg.library_root)
    assert rel in verify_library(st, cfg).missing


def test_audit_reset_drops_phantom_index_row(world, monkeypatch):
    """A copy_in that inserts an index row but whose destination vanishes before
    the audit must have that index row removed on reset (no false ORGANIZED)."""
    files = {"v/a.mp4": b"A" * 300}
    cfg, plan = _organize_plan(world, files, {"v/a.mp4": ("UNIQUE", "bucket:Video")})
    st = world["store"]
    from mlo.report import read_plan
    _, rows, _ = read_plan(plan.path)
    rel = os.path.relpath(rows[0]["dst"], cfg.library_root)

    from mlo import winpath
    real = SafeOps._syscall

    def copy_then_vanish(self, kind, src, dst, pre_size, pre_qh):
        eff = real(self, kind, src, dst, pre_size, pre_qh)   # real copy + index insert
        os.remove(winpath.to_long(dst))                      # dst gone before audit
        return eff

    monkeypatch.setattr(SafeOps, "_syscall", copy_then_vanish)
    r = apply_plan(st, cfg, plan.path, st.start_run("x", [], cfg.config_hash, "t"),
                   execute=True, drive_of=world["drive_of"])
    assert r.audit_failures                        # audit caught the vanish
    assert st.index_get(rel) is None               # phantom row removed (C7)


# ── C8: an unhealable residual re-applies to a STABLE plan_id, no chain ──────

def test_residual_kind_is_stable(world):
    files = {f"v/f{i}.mp4": bytes([65 + i]) * 300 for i in range(2)}
    cfg, plan = _organize_plan(
        world, files, {r: ("UNIQUE", "bucket:Video") for r in files})
    st = world["store"]
    from mlo.report import read_plan
    _, rows, _ = read_plan(plan.path)
    os.remove(rows[0]["src"])                       # permanently missing source

    r1 = apply_plan(st, cfg, plan.path, st.start_run("a", [], cfg.config_hash, "t"),
                    execute=True, drive_of=world["drive_of"])
    assert r1.exit_code == 3 and "organize-residual" in r1.residual_plan
    r2 = apply_plan(st, cfg, r1.residual_plan,
                    st.start_run("b", [], cfg.config_hash, "t"),
                    execute=True, drive_of=world["drive_of"])
    # residual OF a residual keeps the same kind -> same rows -> same plan_id ->
    # same file, not an ever-growing chain (pre-fix: organize-residual-residual)
    assert r2.residual_plan == r1.residual_plan


# ── C9: organize completed via a residual still satisfies the dedup gate ─────

def test_dedup_allowed_after_organize_completes_via_residual(world):
    files = {"v/a.mp4": b"A" * 300, "v/b.mp4": b"B" * 300, "junk/t.tmp": b""}
    cfg = make_cfg(world)
    pre = seed_source(world, cfg, files)
    seed_store(world, cfg, pre, {
        "v/a.mp4": ("UNIQUE", "bucket:Video"),
        "v/b.mp4": ("UNIQUE", "bucket:Video"),
        "junk/t.tmp": ("JUNK", "junk:ext")})
    st = world["store"]
    plan = planmod.build_organize(st, cfg, "eSrc", drive_of=world["drive_of"])
    from mlo.report import read_plan
    _, rows, _ = read_plan(plan.path)
    src_b = next(r["src"] for r in rows if r["dst"].endswith("b.mp4"))
    planned = Path(src_b).read_bytes()
    Path(src_b).write_bytes(b"drift")               # one row drifts

    r1 = apply_plan(st, cfg, plan.path, st.start_run("a", [], cfg.config_hash, "t"),
                    execute=True, drive_of=world["drive_of"])
    assert r1.exit_code == 3
    Path(src_b).write_bytes(planned)                # fix drift, finish via residual
    r2 = apply_plan(st, cfg, r1.residual_plan,
                    st.start_run("b", [], cfg.config_hash, "t"),
                    execute=True, drive_of=world["drive_of"])
    assert r2.exit_code == 0
    # the dedup gate must accept the 'organize-residual' executed plan (C9)
    ded = planmod.build_dedup(st, cfg, "eSrc", drive_of=world["drive_of"])
    assert ded.kind == "dedup" and ded.n_rows == 1  # the JUNK row


# ── C10: a source nested under the library is refused (not silently emptied) ──

def test_source_under_library_refused(tmp_path):
    lib = tmp_path / "lib"
    (lib / "Incoming").mkdir(parents=True)
    cfg = cfgmod.load(_write(tmp_path, f'''
[library]
root = {str(lib)!r}
[[sources]]
name = "inc"
root = {str(lib / "Incoming")!r}
'''))
    with pytest.raises(ConfigError, match="must not live inside the library"):
        cfgmod.validate(cfg, str(tmp_path / "ws"))


def _write(tmp_path, body: str) -> str:
    p = tmp_path / "mlo.toml"
    p.write_text(body, encoding="utf-8")
    return str(p)
