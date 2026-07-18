"""Apply: idempotency, crash injection, drift-as-data (defects L1, L9, L17)."""
from __future__ import annotations

import os
from pathlib import Path

from helpers_plan import make_cfg, seed_source, seed_store
from mlo import plan as planmod
from mlo.apply import apply_plan, reconcile_pending
from mlo.safeops import SafeOps


def ready_organize_plan(world, n_files: int = 6):
    cfg = make_cfg(world)
    files = {f"d{i}/clip{i}.mp4": bytes([65 + i]) * (512 + i) for i in range(n_files)}
    pre = seed_source(world, cfg, files)
    verdicts = {rel: ("UNIQUE", "bucket:Video") for rel in files}
    seed_store(world, cfg, pre, verdicts)
    res = planmod.build_organize(world["store"], cfg, "eSrc")
    return cfg, res


def test_dry_run_then_execute_then_noop(world):
    cfg, plan = ready_organize_plan(world)
    st = world["store"]

    dry = apply_plan(st, cfg, plan.path, st.start_run("a", [], cfg.config_hash, "t"),
                     execute=False, drive_of=world["drive_of"])
    assert dry.counts == {"would_do": plan.n_rows}
    assert dry.exit_code == 0 and not dry.residual_plan

    ex = apply_plan(st, cfg, plan.path, st.start_run("b", [], cfg.config_hash, "t"),
                    execute=True, drive_of=world["drive_of"])
    assert ex.counts == {"done": plan.n_rows}
    assert ex.exit_code == 0
    assert st.artifact_get(f"plan:{plan.plan_id}").status == "executed"
    assert st.index_count() == plan.n_rows          # copies entered the index

    again = apply_plan(st, cfg, plan.path, st.start_run("c", [], cfg.config_hash, "t"),
                       execute=True, drive_of=world["drive_of"])
    assert again.counts == {"skipped_done": plan.n_rows}
    assert again.exit_code == 0                     # pure no-op, L1


def test_crash_midrun_rerun_creates_no_duplicates(world, monkeypatch):
    cfg, plan = ready_organize_plan(world, n_files=8)
    st = world["store"]

    real = SafeOps._syscall
    calls = {"n": 0}

    def explode_after_3(self, kind, src, dst, pre_size, pre_qh):
        calls["n"] += 1
        if calls["n"] > 3:
            raise OSError("simulated power loss")
        return real(self, kind, src, dst, pre_size, pre_qh)

    monkeypatch.setattr(SafeOps, "_syscall", explode_after_3)
    crashed = apply_plan(st, cfg, plan.path,
                         st.start_run("x", [], cfg.config_hash, "t"),
                         execute=True, drive_of=world["drive_of"])
    assert crashed.exit_code == 3
    assert crashed.counts.get("done", 0) == 3
    assert crashed.counts.get("failed", 0) == plan.n_rows - 3

    monkeypatch.setattr(SafeOps, "_syscall", real)
    healed = apply_plan(st, cfg, plan.path,
                        st.start_run("y", [], cfg.config_hash, "t"),
                        execute=True, drive_of=world["drive_of"])
    assert healed.exit_code == 0
    assert healed.counts.get("skipped_done", 0) == 3
    assert healed.counts.get("done", 0) == plan.n_rows - 3

    lib_files = [p for p in Path(cfg.library_root).rglob("*") if p.is_file()]
    assert len(lib_files) == plan.n_rows            # zero duplicates, zero misses
    assert st.index_count() == plan.n_rows


def test_pending_row_reconciliation(world):
    """A crash between journal-intent and the done-mark: dst verified on disk."""
    cfg, plan = ready_organize_plan(world, n_files=1)
    st = world["store"]
    from mlo.report import read_plan
    _, rows, _ = read_plan(plan.path)
    row = rows[0]

    # simulate: op acted on disk but never got its done-mark
    Path(row["dst"]).parent.mkdir(parents=True, exist_ok=True)
    Path(row["dst"]).write_bytes(Path(row["src"]).read_bytes())
    st.journal_intent("crashed-run", plan.plan_id, row["op_id"], row["kind"],
                      row["src"], row["dst"], row["pre"]["size"],
                      row["pre"]["quick_hash"])

    n = reconcile_pending(st, cfg.library_root)
    assert n == 1
    assert st.op_state(row["op_id"]) == "done"
    # the reconciled copy_in must have entered the library index (defect L2)
    import os
    rel = os.path.relpath(row["dst"], cfg.library_root)
    assert st.index_get(rel) is not None


def test_reconcile_pending_verbose_list_includes_verified_copy_in(world):
    """Regression: the verbose list must record EVERY resolved op, including
    the copied-and-verified branch, whose `continue` used to skip appending
    to it (a real gap P21/C4 found: the count `n` was right, the op_id list
    silently wasn't)."""
    cfg, plan = ready_organize_plan(world, n_files=1)
    st = world["store"]
    from mlo.report import read_plan
    _, rows, _ = read_plan(plan.path)
    row = rows[0]
    Path(row["dst"]).parent.mkdir(parents=True, exist_ok=True)
    Path(row["dst"]).write_bytes(Path(row["src"]).read_bytes())
    st.journal_intent("crashed-run", plan.plan_id, row["op_id"], row["kind"],
                      row["src"], row["dst"], row["pre"]["size"],
                      row["pre"]["quick_hash"])

    seen: list = []
    n = reconcile_pending(st, cfg.library_root, verbose=seen)
    assert n == 1
    assert seen == [row["op_id"]]


def test_apply_verbose_prints_reconciled_ops_to_stderr(world, capsys):
    """P21/C4/C6: `-v` surfaces exactly which ops a crash-reconcile resolved,
    on stderr — apply(verbose=False) stays silent about it beyond the count."""
    cfg, plan = ready_organize_plan(world, n_files=1)
    st = world["store"]
    from mlo.report import read_plan
    _, rows, _ = read_plan(plan.path)
    row = rows[0]
    Path(row["dst"]).parent.mkdir(parents=True, exist_ok=True)
    Path(row["dst"]).write_bytes(Path(row["src"]).read_bytes())
    st.journal_intent("crashed-run", plan.plan_id, row["op_id"], row["kind"],
                      row["src"], row["dst"], row["pre"]["size"],
                      row["pre"]["quick_hash"])

    run_id = st.start_run("apply", [], cfg.config_hash, "t")
    res = apply_plan(st, cfg, plan.path, run_id, execute=True, verbose=True)
    assert any("reconciled 1 pending ops" in w for w in res.warnings)
    err = capsys.readouterr().err
    assert f"reconciled: {row['op_id']}" in err


def test_dryrun_reports_missing_sources(world):
    cfg, plan = ready_organize_plan(world, n_files=3)
    from mlo.report import read_plan
    _, rows, _ = read_plan(plan.path)
    os.remove(rows[0]["src"])                        # test-side mutation
    st = world["store"]
    dry = apply_plan(st, cfg, plan.path, st.start_run("d", [], cfg.config_hash, "t"),
                     execute=False, drive_of=world["drive_of"])
    assert dry.counts.get("skipped_drift") == 1      # L17: rehearsal is honest
    assert dry.counts.get("would_do") == 2
    assert any("source missing" in d["detail"] for d in dry.drift)


def test_drift_becomes_structured_skip_and_residual(world):
    cfg, plan = ready_organize_plan(world, n_files=3)
    from mlo.report import read_plan
    _, rows, _ = read_plan(plan.path)
    Path(rows[1]["src"]).write_bytes(b"changed since planning - different bytes")
    st = world["store"]
    ex = apply_plan(st, cfg, plan.path, st.start_run("e", [], cfg.config_hash, "t"),
                    execute=True, drive_of=world["drive_of"])
    assert ex.counts.get("done") == 2
    assert ex.counts.get("skipped_drift") == 1
    assert ex.exit_code == 3 and ex.residual_plan
    assert os.path.exists(ex.residual_plan)
