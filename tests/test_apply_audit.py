"""The audit tail (L5) and resolved-state journaling (L16)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from helpers_plan import make_cfg, seed_source, seed_store
from mlo import plan as planmod
from mlo.apply import apply_plan
from mlo.safeops import SafeOps


def ready_plan(world, n=2):
    cfg = make_cfg(world)
    files = {f"v/f{i}.mp4": bytes([70 + i]) * 900 for i in range(n)}
    pre = seed_source(world, cfg, files)
    seed_store(world, cfg, pre,
               {rel: ("UNIQUE", "bucket:Video") for rel in files})
    return cfg, planmod.build_organize(world["store"], cfg, "eSrc")


def test_postconditions_checked_every_apply(world, monkeypatch):
    """An op that journals 'done' without producing its outcome is caught by the
    audit, residual-planned, and the run exits 3 — 'complete but wrong' is not a
    reachable status (defect L5)."""
    cfg, plan = ready_plan(world)
    st = world["store"]

    def liar(self, kind, src, dst, pre_size, pre_qh):
        return None                                   # acts on nothing, reports fine

    monkeypatch.setattr(SafeOps, "_syscall", liar)
    res = apply_plan(st, cfg, plan.path, st.start_run("l", [], cfg.config_hash, "t"),
                     execute=True, drive_of=world["drive_of"])
    assert res.counts.get("done") == plan.n_rows      # the lie
    assert len(res.audit_failures) == plan.n_rows     # the audit catching it
    assert res.exit_code == 3 and res.status == "completed_with_residuals"
    assert res.residual_plan and os.path.exists(res.residual_plan)


def test_residuals_emit_plan_and_exit_3(world):
    cfg, plan = ready_plan(world, n=3)
    from mlo.report import read_plan
    _, rows, _ = read_plan(plan.path)
    os.remove(rows[2]["src"])
    st = world["store"]
    res = apply_plan(st, cfg, plan.path, st.start_run("r", [], cfg.config_hash, "t"),
                     execute=True, drive_of=world["drive_of"])
    assert res.exit_code == 3
    _, residual_rows, _ = read_plan(res.residual_plan)
    assert len(residual_rows) == 1
    assert residual_rows[0]["op_id"] == rows[2]["op_id"]


def test_manifest_records_resolved_destination(world):
    """L16: what the journal says happened is where the file actually is."""
    cfg, plan = ready_plan(world)
    st = world["store"]
    apply_plan(st, cfg, plan.path, st.start_run("m", [], cfg.config_hash, "t"),
               execute=True, drive_of=world["drive_of"])
    from mlo.report import read_plan
    _, rows, _ = read_plan(plan.path)
    journal = {op["op_id"]: op for op in st.export_ops()}
    for r in rows:
        op = journal[r["op_id"]]
        assert op["state"] == "done"
        assert op["dst_display"] == r["dst"]          # never an unrecorded slot
        assert os.path.exists(r["dst"])


def test_summary_and_views_written(world):
    cfg, plan = ready_plan(world)
    st = world["store"]
    run = st.start_run("s", [], cfg.config_hash, "t")
    res = apply_plan(st, cfg, plan.path, run, execute=True,
                     drive_of=world["drive_of"])
    assert res.summary_path and os.path.exists(res.summary_path)
    summary = json.loads(Path(res.summary_path).read_text(encoding="utf-8"))
    assert summary["schema"] == "mlo.summary/1"
    assert summary["suggested_next"][0]["cmd"].startswith("mlo ")
    applied_csv = Path(res.summary_path).with_name("applied.csv")
    assert applied_csv.exists()
    first = applied_csv.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("# {")                    # provenance comment row
