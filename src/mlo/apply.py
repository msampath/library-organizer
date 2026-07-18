"""Apply: the idempotent execute loop with the audit tail.

Per row: journal gate -> precondition re-verify -> kernel op (all inside
SafeOps, one code path for rehearse/execute — defects L1, L9). After the loop,
the POST-CONDITION AUDIT runs unconditionally (defect L5): unmet outcomes make
the run `completed_with_residuals` (exit 3) and auto-emit a residual plan.

Crash recovery happens before any row is attempted: leftover 'pending' journal
rows are reconciled against disk (done / retryable / ambiguous-never-guessed).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from . import fingerprint, report, winpath
from .config import Config
from .safeops import PathPolicy, SafeOps, _mtime_ns
from .store import Store


class DisposeNotConfirmed(Exception):
    """P21/C2: executing a 'dispose' plan needs --confirm-dispose <exact row
    count> — a typed, scriptable confirmation (never an interactive prompt,
    matching every other mlo command) so disposal is never armed by habit.
    CLI maps to exit 2."""


@dataclass
class ApplyResult:
    plan_id: str
    plan_path: str
    executed: bool
    counts: dict = field(default_factory=dict)
    drift: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    audit_failures: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    residual_plan: str | None = None
    status: str = "completed"
    exit_code: int = 0
    summary_path: str | None = None


def _policy(cfg: Config, drive_of=None) -> PathPolicy:
    kwargs = {}
    if drive_of is not None:
        kwargs["drive_of"] = drive_of
    return PathPolicy(cfg.protected_substrings, cfg.protected_drives,
                      dict(cfg.staging), cfg.library_root, **kwargs)


def _reconcile_index_effect(kind: str, src: str, dst: str, lib: str,
                            size: int, qh: str, run_id: str):
    """The library-index effect a reconciled-done op must apply, mirroring
    SafeOps._syscall so the index cannot desync after a crash (defect L2/L7)."""
    src_in = winpath.is_under(src, lib)
    dst_in = winpath.is_under(dst, lib)
    rel = lambda p: os.path.relpath(winpath.from_long(p), winpath.from_long(lib))
    if kind == "copy_in" or (not src_in and dst_in):
        return ("insert", rel(dst), size, qh, _mtime_ns(winpath.to_long(dst)), run_id)
    if src_in and dst_in:
        return ("move", rel(src), rel(dst))
    if src_in:
        return ("delete", rel(src))
    return None


def reconcile_pending(store: Store, library_root: str,
                      verbose: list | None = None) -> int:
    """Resolve journal rows left 'pending' by a crash (architecture §4). A
    promotion to 'done' carries the same library-index effect the kernel would
    have applied, so the index never lags the filesystem (defect L2). Never
    guesses: both-present goes to failed-with-detail for a human (L14)."""
    n = 0
    for op in store.pending_ops():
        n += 1
        src_ok = os.path.exists(winpath.to_long(op["src"]))
        dst_ok = os.path.exists(winpath.to_long(op["dst"]))
        kind = op["kind"]
        if kind == "rmdir_empty":
            # rmdir journals src == dst == the directory; the generic
            # branches below assume distinct ends and would record a
            # succeeded rmdir as 'both ends missing' and an untouched one
            # as hand-resolve AMBIGUOUS. Directory gone = the op succeeded;
            # directory present = plainly retryable. No index effect either
            # way (rmdir never touches the file index).
            if src_ok:
                store.complete_op(op["op_id"], "failed",
                                  "crash reconcile: retryable")
            else:
                store.complete_op(op["op_id"], "done",
                                  "crash reconcile: dir removed")
            if verbose is not None:
                verbose.append(op["op_id"])
            continue
        if kind == "dispose":
            # dispose journals src == dst == the file (single-path, like
            # rmdir_empty): present = the syscall never ran, retryable; gone
            # = the recycle/trash succeeded before the crash. No index
            # effect either way (staging content is never indexed, L7 n/a).
            if src_ok:
                store.complete_op(op["op_id"], "failed",
                                  "crash reconcile: retryable")
            else:
                store.complete_op(op["op_id"], "done",
                                  "crash reconcile: disposed")
            if verbose is not None:
                verbose.append(op["op_id"])
            continue
        if dst_ok and not src_ok and kind in ("stage_move", "move_within", "copy_in"):
            try:
                size, qh = fingerprint.quick(op["dst"])
            except OSError:
                store.complete_op(op["op_id"], "failed",
                                  "crash reconcile: dst unreadable")
                if verbose is not None:      # every resolved op is recorded (C65)
                    verbose.append(op["op_id"])
                continue
            if op["pre_quick_hash"] in (None, qh):
                store.complete_op(
                    op["op_id"], "done", "crash reconcile: dst verified present",
                    index_effect=_reconcile_index_effect(
                        kind, op["src"], op["dst"], library_root, size, qh,
                        op["op_id"]))
            else:
                store.complete_op(op["op_id"], "failed",
                                  "crash reconcile: dst hash mismatch")
        elif src_ok and not dst_ok:
            store.complete_op(op["op_id"], "failed", "crash reconcile: retryable")
        elif src_ok and dst_ok:
            if kind == "copy_in":
                # copies keep their source; verify the copy like the kernel would
                try:
                    size, qh = fingerprint.quick(op["dst"])
                    if op["pre_quick_hash"] in (None, qh):
                        store.complete_op(
                            op["op_id"], "done", "crash reconcile: copy verified",
                            index_effect=_reconcile_index_effect(
                                kind, op["src"], op["dst"], library_root, size, qh,
                                op["op_id"]))
                        if verbose is not None:
                            verbose.append(op["op_id"])
                        continue
                except OSError:
                    pass
            store.complete_op(
                op["op_id"], "failed",
                "crash reconcile: AMBIGUOUS — src and dst both exist; "
                "resolve by hand, never guessed (L14)")
        else:
            store.complete_op(op["op_id"], "failed",
                              "crash reconcile: both ends missing")
        if verbose is not None:
            verbose.append(op["op_id"])
    return n


def apply_plan(store: Store, cfg: Config, plan_path: str, run_id: str,
               execute: bool, drive_of=None, verbose: bool = False,
               confirm_dispose: int | None = None, disposer=None) -> ApplyResult:
    header, rows, plan_id = report.read_plan(plan_path)

    # The C68 typed-confirmation gate covers dispose AND dispose-residual —
    # a residual's rows still carry kind='dispose' and the real disposer, so
    # an exact-match on 'dispose' alone would let any partially-failed run's
    # residual execute real disposal unconfirmed (super-review finding H2).
    _is_dispose_plan = header.get("kind", "") in ("dispose", "dispose-residual")
    if execute and _is_dispose_plan and confirm_dispose != len(rows):
        raise DisposeNotConfirmed(
            f"this dispose plan has {len(rows)} row(s); pass "
            f"--confirm-dispose {len(rows)} to execute it (typed row-count "
            f"confirmation — disposal is never routine)")

    res = ApplyResult(plan_id=plan_id, plan_path=plan_path, executed=execute)

    if header.get("config_hash") != cfg.config_hash:
        res.warnings.append("config changed since this plan was built")
    for inp in header.get("inputs", []):
        a = store.artifact_get(inp["artifact_id"])
        if a is None or a.status != "fresh":
            res.warnings.append(
                f"input {inp['artifact_id']} has gone stale since planning "
                f"(per-row re-verification is the backstop)")

    if execute:
        reconciled: list | None = [] if verbose else None
        n = reconcile_pending(store, cfg.library_root, verbose=reconciled)
        if n:
            res.warnings.append(f"reconciled {n} pending ops from a prior crash")
        if reconciled:
            for op_id in reconciled:
                print(f"  reconciled: {op_id} ({store.op_state(op_id)})",
                     file=sys.stderr)

    kernel = SafeOps(_policy(cfg, drive_of), store, run_id, execute, plan_id,
                     disposer=disposer)
    counts: dict[str, int] = {}
    residual_rows: list[dict] = []
    acted_rows: list[dict] = []          # rows THIS run transitioned to 'done'

    for row in rows:
        pre = row.get("pre", {})
        op = kernel._run(row["kind"], row["src"], row["dst"],
                         pre.get("size"), pre.get("quick_hash"))
        counts[op.status] = counts.get(op.status, 0) + 1
        # Every non-terminal-success outcome joins the residual plan so it can
        # be retried once its cause is fixed. skipped_protected is included so a
        # protected-content row can never masquerade as a swept source (L11/L12).
        if op.status == "skipped_drift":
            res.drift.append({"src": row["src"], "dst": row["dst"],
                              "detail": op.detail})
            residual_rows.append(row)
        elif op.status == "skipped_protected":
            res.failures.append({"src": row["src"], "dst": row["dst"],
                                 "detail": f"protected: {op.detail}"})
            residual_rows.append(row)
        elif op.status == "failed":
            res.failures.append({"src": row["src"], "dst": row["dst"],
                                 "detail": op.detail})
            residual_rows.append(row)
        elif op.status == "done":
            acted_rows.append(row)

    res.counts = counts

    # ── the audit tail (L5): post-conditions of ops THIS run acted on ───────
    # Scoped to acted_rows, NOT every 'done' row in the journal: a re-apply
    # no-ops prior-run ops (skipped_done), and re-auditing those against a disk
    # that legitimately changed since (external deletion, disposed staging)
    # would corrupt durable state — the defect the 2nd-order review found (C7).
    if execute:
        for row in acted_rows:
            ldst = winpath.to_long(row["dst"])
            lsrc = winpath.to_long(row["src"])
            bad = None
            if row["kind"] in ("stage_move", "move_within"):
                if not os.path.exists(ldst):
                    bad = "post: destination missing"
                elif os.path.exists(lsrc):
                    bad = "post: source still present after move"
            elif row["kind"] == "copy_in":
                if not os.path.exists(ldst):
                    bad = "post: copy missing"
            elif row["kind"] == "dispose":
                if os.path.exists(lsrc):
                    bad = "post: file still present after dispose"
            if bad == "post: source still present after move":
                # The move itself completed (dst verified present) — the src
                # is an external resurrection, not a lying 'done'. Keep the
                # op and its CORRECT dst index row (resetting would delete a
                # true row and the retry would drift forever on 'destination
                # occupied'); surface the resurrected src for the human.
                res.audit_failures.append(
                    {"src": row["src"], "dst": row["dst"], "detail": bad})
            elif bad:
                # Reset the lying 'done' row so the residual can retry it with
                # its own op_id (the journal gate would otherwise no-op it — L5),
                # and drop any library-index row the bogus 'done' inserted so it
                # can't drive a false ORGANIZED verdict (C7).
                drop = (os.path.relpath(row["dst"], cfg.library_root)
                        if winpath.is_under(row["dst"], cfg.library_root) else None)
                store.mark_retryable(row["op_id"], f"audit reset: {bad}", drop)
                res.audit_failures.append(
                    {"src": row["src"], "dst": row["dst"], "detail": bad})
                residual_rows.append(row)

    if execute and residual_rows:
        # Stable residual kind: a residual OF a residual keeps the same name, so
        # an unhealable residual (e.g. all-protected rows) re-applies to the same
        # plan_id instead of spawning an ever-longer chain of artifacts (C8).
        base_kind = header.get("kind", "residual")
        residual_kind = base_kind if base_kind.endswith("-residual") \
            else base_kind + "-residual"
        rpath, rid = report.write_plan(
            store.workspace, residual_kind,
            header.get("source", "?"), cfg.config_hash,
            header.get("inputs", []), residual_rows)
        store.artifact_register(
            f"plan:{rid}", "plan",
            {"kind": header.get("kind", "residual"),
             "source": header.get("source", "?"), "path": rpath},
            cfg.config_hash, run_id, status="fresh")
        res.residual_plan = rpath
        res.status = "completed_with_residuals"
        res.exit_code = 3
    elif execute:
        store.artifact_set_status(f"plan:{plan_id}", "executed")

    # ── views + summary ─────────────────────────────────────────────────────
    suggested = []
    if res.residual_plan:
        # A dispose residual's suggested command must carry the typed
        # row-count confirmation — suggesting the bare form would teach the
        # exact habit the C68 gate exists to prevent.
        confirm = (f" --confirm-dispose {len(residual_rows)}"
                   if _is_dispose_plan else "")
        suggested.append({
            "cmd": f"mlo apply \"{res.residual_plan}\" --execute{confirm}",
            "why": f"{len(residual_rows)} rows did not complete"})
    elif execute:
        suggested.append({"cmd": "mlo verify library",
                          "why": "plan fully applied — confirm library state"})
    else:
        suggested.append({"cmd": f"mlo apply \"{plan_path}\" --execute",
                          "why": "rehearsal complete"})

    report.export_csv(
        store.workspace, run_id, "applied",
        ["op_id", "kind", "src", "dst", "state", "detail"],
        ({"op_id": r["op_id"], "kind": r["kind"], "src": r["src"],
          "dst": r["dst"],
          "state": store.op_state(r["op_id"]) or ("would_do" if not execute else "?"),
          "detail": ""} for r in rows),
        {"run": run_id, "plan_id": plan_id, "journal_pos": store.journal_pos(),
         "config_hash": cfg.config_hash})
    res.summary_path = report.write_summary(store.workspace, run_id, {
        "command": f"apply {'--execute' if execute else '(dry-run)'}",
        "plan_id": plan_id,
        "config_hash": cfg.config_hash,
        "counts": {"by_op_state": counts},
        "drift": res.drift[:200],
        "residuals": [r["op_id"] for r in residual_rows][:500],
        "audit_failures": res.audit_failures[:200],
        "warnings": res.warnings,
        "exit_code": res.exit_code,
        "suggested_next": suggested,
    })
    return res
