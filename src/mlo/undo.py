"""mlo undo (P21/C1): reverse a run's placement ops through the NORMAL plan
path — dst becomes src, src becomes dst, sealed and applied through the same
kernel gates as any other plan (L1/L12/L16/L17). No special-cased execution
exists here; this module only builds a plan.

What is auto-reversible: `move_within` ops only. The kernel has no primitive
for the other directions — `copy_in` has no inverse (no delete exists), and
a `stage_move`'s reversal would move FROM staging back to an arbitrary
same-drive location, which neither `move_within` (both ends in-library) nor
`stage_move` (dst under staging) can express. Those ops are counted in the
plan's notes, never silently dropped; a `dispose` op's recovery path is the
OS Recycle Bin / trash itself. Rows whose target file has since moved, been
replaced with different content, or become unreadable are likewise reported
and skipped — the plan can only contain what it can actually reverse.

Paths come from the journal's LOSSLESS BLOB columns (store.export_ops), never
the *_display strings — a lossy display path would make surrogate-named
files silently unreachable (L10).
"""
from __future__ import annotations

import os

from . import fingerprint, winpath
from .config import Config
from .plan import PlanError, PlanResult, _register_plan, _row
from .store import Store


def build_undo(store: Store, cfg: Config, run_id: str, drive_of=None) -> PlanResult:
    """A reverse plan for run `run_id`'s DONE move_within ops, newest first
    (a LIFO undo: if a run moved a file A -> B -> C, the reverse plan's rows
    are C -> B then B -> A, applied in that order)."""
    if store.get_run(run_id) is None:
        raise PlanError(f"no such run: {run_id}")

    ops = list(store.export_ops(run_id))
    done = [o for o in ops if o["state"] == "done"]
    reversible = [o for o in done if o["kind"] == "move_within"]
    reversible.sort(key=lambda o: o["rowid"], reverse=True)
    n_copy_in = sum(1 for o in done if o["kind"] == "copy_in")
    n_staged = sum(1 for o in done if o["kind"] == "stage_move")
    n_disposed = sum(1 for o in done if o["kind"] == "dispose")

    rows: list[dict] = []
    produced_dsts: set[str] = set()   # dsts of rows already queued (LIFO chains)
    n_missing = n_replaced = n_unreadable = 0
    for op in reversible:
        src = op["dst"]              # where the file lives now (lossless, L10)
        dst = op["src"]              # ...and where undo sends it back
        if os.path.exists(winpath.to_long(src)):
            try:
                size, qh = fingerprint.quick(src)
            except OSError:
                n_unreadable += 1
                continue
            # The journaled pre-fingerprint is the file's content when the
            # engine moved it (a rename preserves bytes) — a mismatch means
            # something REPLACED the file at that path since, and moving the
            # impostor back would relocate the wrong file.
            if ((op["pre_size"] is not None and size != op["pre_size"])
                    or (op["pre_quick_hash"] is not None
                        and qh != op["pre_quick_hash"])):
                n_replaced += 1
                continue
        elif os.path.normcase(src) in produced_dsts:
            # A later hop in this SAME undo plan (executed earlier, since
            # rows run in list order) will put the file here first — an
            # A->B->C chain undoes as C->B (fingerprinted) then B->A (not
            # yet on disk at build time, so no drift check on this hop).
            size, qh = None, None
        else:
            n_missing += 1
            continue
        rows.append(_row("move_within", src, dst, size, qh,
                         "UNDO", f"undo:{run_id}"))
        produced_dsts.add(os.path.normcase(dst))

    notes = [f"reversing {len(rows)} of {len(reversible)} reversible op(s) "
             f"from run {run_id}"]
    if n_missing:
        notes.append(f"{n_missing} op(s) skipped: file no longer present at "
                     f"its post-run location (moved or disposed since)")
    if n_replaced:
        notes.append(f"{n_replaced} op(s) skipped: the file at the post-run "
                     f"location no longer matches the content the engine "
                     f"moved there (replaced since — moving it back would "
                     f"relocate the wrong file)")
    if n_unreadable:
        notes.append(f"{n_unreadable} op(s) skipped: file unreadable")
    if n_copy_in:
        notes.append(f"{n_copy_in} copy_in op(s) cannot be undone — the "
                     f"safety kernel has no delete primitive; the copied "
                     f"file remains in the library")
    if n_staged:
        notes.append(f"{n_staged} stage_move op(s) cannot be auto-reversed — "
                     f"the kernel has no unstage primitive (staging -> origin "
                     f"is neither move_within nor stage_move); the files "
                     f"remain in staging, untouched")
    if n_disposed:
        notes.append(f"{n_disposed} dispose op(s) are recoverable through "
                     f"the OS Recycle Bin / trash, not through mlo")
    if not rows:
        notes.append("nothing to undo")

    path, plan_id, cap_notes = _register_plan(
        store, cfg, "undo", run_id, [], rows, drive_of)
    return PlanResult(path, plan_id, len(rows), "undo", run_id, notes + cap_notes)
