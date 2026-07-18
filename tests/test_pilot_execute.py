"""mlo pilot --execute (Pass 2): approved sections execute in dependency order
with bounded convergence; approvals are hash-bound to the exact reviewed
proposal (the approve-X-execute-X gate, L16 class); rejections are sticky —
convergence re-plans can never resurrect a rejected move."""
from __future__ import annotations

import json
import os

import pytest

from conftest import make_file
from helpers import make_cfg
from mlo import pilot as pilotmod
from mlo import report
from mlo.config import ConfigError
from mlo.pilot import ApprovalsError
from mlo.report import read_proposal

TAX = {"Video": (".mp4", ".mkv"), "Audio": (".mp3",),
       "Photos": (".jpg",), "Documents": (".pdf",)}


def n(*segs):
    return os.sep.join(segs)


def seed_world(world):
    lib, src = world["lib"], world["E"]
    make_file(lib / "Audio/Music/Hindi/Album/track.mp3", b"TWIN-BYTES" * 40)
    make_file(lib / "Video/eSrc/films/Heat.(1995).mkv", b"HEAT" * 100)
    make_file(src / "films/Sivaji.The.Boss.(2007).mkv", b"SIVAJI" * 100)
    make_file(src / "music/track.mp3", b"TWIN-BYTES" * 40)
    make_file(src / "Thumbs.db", b"x")


def analyze(world, **kw):
    cfg = make_cfg(world, taxonomy=TAX)
    st = world["store"]
    run = st.start_run("pilot-a", [], cfg.config_hash, "t")
    res = pilotmod.analyze(st, cfg, run, drive_of=world["drive_of"], **kw)
    return cfg, st, res


def execute(world, cfg, st, proposal_path, approvals, **kw):
    run = st.start_run("pilot-x", [], cfg.config_hash, "t")
    return pilotmod.execute(st, cfg, run, proposal_path, approvals,
                            drive_of=world["drive_of"], **kw)


def outcomes_by_id(res):
    return {o.id: o for o in res.outcomes}


def test_approve_all_end_to_end_converges_and_verifies(world):
    seed_world(world)
    cfg, st, ares = analyze(world)
    proposal = read_proposal(ares.proposal_path)
    approvals = pilotmod.approve_all(proposal)

    res = execute(world, cfg, st, ares.proposal_path, approvals)
    outs = outcomes_by_id(res)

    assert res.exit_code == 0
    # the unique movie entered the library, clean-named (organize)
    assert (world["lib"] / "Video" / "Movies" / "Other"
            / "Sivaji The Boss (2007)" / "Sivaji The Boss (2007).mkv").exists()
    # the gated dedup built AFTER organize and staged the twin + junk out
    assert outs["dedup:e"].status == "converged"
    assert not (world["E"] / "music" / "track.mp3").exists()
    staged_root = world["E"] / "Delete"
    assert any(staged_root.rglob("track.mp3"))
    # the misplaced library movie re-homed (reorganize converged)
    assert outs["reorganize:library"].status == "converged"
    assert not (world["lib"] / "Video" / "eSrc" / "films"
                / "Heat.(1995).mkv").exists()
    # verify tail ran clean
    assert res.verify["library"]["missing"] == 0
    assert res.verify["blocking"] is False

    # re-execute with the same approvals: idempotent, everything skipped_done
    res2 = execute(world, cfg, st, ares.proposal_path, approvals)
    assert res2.exit_code == 0
    done_again = sum(c.get("done", 0)
                     for o in res2.outcomes for c in o.counts_by_cycle)
    assert done_again == 0


def test_partial_approval_executes_exactly_the_approved_cluster(world):
    lib = world["lib"]
    make_file(lib / "Video/mess/tamil/Roja.(1992).mkv", b"ROJA" * 60)
    make_file(lib / "Video/mess/Heat.(1995).mkv", b"HEAT" * 60)
    cfg, st, ares = analyze(world)
    proposal = read_proposal(ares.proposal_path)
    sec = next(s for s in proposal["sections"]
               if s["id"] == "reorganize:library")
    assert sec["n_rows"] == 2 and len(sec["clusters"]) == 2   # Tamil vs Other
    reject = next(c["id"] for c in sec["clusters"] if "Heat" in
                  json.dumps(c["sample"]))
    approvals = {
        "schema": pilotmod.APPROVALS_SCHEMA,
        "proposal_sha256": proposal["proposal_sha256"],
        "decisions": {"reorganize:library":
                      {"default": "approve", "clusters": {reject: "reject"}}},
        "converge": True,
    }
    res = execute(world, cfg, st, ares.proposal_path, approvals)
    out = outcomes_by_id(res)["reorganize:library"]
    assert out.status == "converged"
    assert out.rejected_dropped >= 1
    # approved cluster moved; rejected cluster untouched AND not resurrected
    # by the convergence re-plan (sticky rejection)
    assert not (lib / "Video/mess/tamil/Roja.(1992).mkv").exists()
    assert (lib / "Video" / "Movies" / "Tamil" / "Roja (1992)"
            / "Roja (1992).mkv").exists()
    assert (lib / "Video/mess/Heat.(1995).mkv").exists()


def test_approvals_bind_to_proposal(world):
    """The approve-X-execute-X gate: approvals carry the proposal hash; a
    mismatch refuses outright (ledger entry C25)."""
    seed_world(world)
    cfg, st, ares = analyze(world)
    approvals = {"schema": pilotmod.APPROVALS_SCHEMA,
                 "proposal_sha256": "0" * 64,
                 "decisions": {"organize:e": "approve"}}
    with pytest.raises(ApprovalsError, match="DIFFERENT proposal"):
        execute(world, cfg, st, ares.proposal_path, approvals)


def test_config_change_between_passes_refuses(world):
    seed_world(world)
    cfg, st, ares = analyze(world)
    proposal = read_proposal(ares.proposal_path)
    approvals = pilotmod.approve_all(proposal)
    import dataclasses
    cfg2 = dataclasses.replace(cfg, config_hash="different-hash")
    with pytest.raises(ConfigError, match="config changed"):
        execute(world, cfg2, st, ares.proposal_path, approvals)


def test_unlisted_sections_default_to_reject(world):
    seed_world(world)
    cfg, st, ares = analyze(world)
    proposal = read_proposal(ares.proposal_path)
    approvals = {"schema": pilotmod.APPROVALS_SCHEMA,
                 "proposal_sha256": proposal["proposal_sha256"],
                 "decisions": {}}                    # approve NOTHING
    res = execute(world, cfg, st, ares.proposal_path, approvals)
    assert all(o.status in ("rejected",) for o in res.outcomes
               if o.id in ("organize:e", "dedup:e", "reorganize:library"))
    # nothing moved
    assert (world["E"] / "films/Sivaji.The.Boss.(2007).mkv").exists()
    assert (world["lib"] / "Video/eSrc/films/Heat.(1995).mkv").exists()


def test_residual_retry_recovers_a_transient_failure(world, monkeypatch):
    """One kernel op fails transiently -> residual plan -> retried within the
    cycle -> converged. The crash-injection precedent applied to Pass 2."""
    seed_world(world)
    cfg, st, ares = analyze(world)
    proposal = read_proposal(ares.proposal_path)
    approvals = pilotmod.approve_all(proposal)

    import mlo.safeops as safeops
    real = safeops.SafeOps._syscall
    state = {"failed": False}

    def flaky(self, kind, src, dst, pre_size, pre_quick_hash):
        if not state["failed"] and kind == "move_within":
            state["failed"] = True
            raise OSError("transient lock")
        return real(self, kind, src, dst, pre_size, pre_quick_hash)

    monkeypatch.setattr(safeops.SafeOps, "_syscall", flaky)
    res = execute(world, cfg, st, ares.proposal_path, approvals)
    out = outcomes_by_id(res)["reorganize:library"]
    assert out.status == "converged"
    assert res.exit_code == 0
    assert not (world["lib"] / "Video/eSrc/films/Heat.(1995).mkv").exists()


def test_convergence_bound_reports_honestly(world, monkeypatch):
    """A builder that keeps emitting rows stops at max_cycles with an honest
    'residual' outcome and exit 3 — never an infinite loop, never a fake
    success."""
    seed_world(world)
    cfg, st, ares = analyze(world)
    proposal = read_proposal(ares.proposal_path)
    approvals = pilotmod.approve_all(proposal)

    import mlo.apply as applymod
    from mlo.apply import ApplyResult
    real = applymod.apply_plan

    def never_done(store, cfg2, plan_path, run_id, execute=False, drive_of=None):
        if execute and "reorganize" in os.path.basename(plan_path):
            # simulate an op that fails every time: nothing moves, the whole
            # plan stays residual — the builder re-plans the same rows forever
            return ApplyResult(plan_id="x", plan_path=plan_path, executed=True,
                               counts={"failed": 1}, residual_plan=plan_path,
                               exit_code=3)
        return real(store, cfg2, plan_path, run_id, execute=execute,
                    drive_of=drive_of)

    monkeypatch.setattr(pilotmod.applymod, "apply_plan", never_done)
    res = execute(world, cfg, st, ares.proposal_path, approvals, max_cycles=2)
    out = outcomes_by_id(res)["reorganize:library"]
    assert out.status == "residual"
    assert out.cycles <= 2
    assert res.exit_code == 3


def test_approve_all_synthesis_covers_ready_and_gated_only(world):
    seed_world(world)
    cfg, st, ares = analyze(world)
    proposal = read_proposal(ares.proposal_path)
    approvals = pilotmod.approve_all(proposal)
    statuses = {s["id"]: s["status"] for s in proposal["sections"]}
    for sid in approvals["decisions"]:
        assert statuses[sid] in ("ready", "gated")


# ── P16: Pass-1 convergence rehearsal (sealed projection) ────────────────────

def _seed_c21_unblock(world):
    """A reorganize move that is C21-blocked by a fingerprint twin until
    dedup-library stages the twin out. Naive Pass-1 reorganize = 0 rows;
    the projection (dedup -> reorganize on a scratch index) = 1."""
    lib = world["lib"]
    twin = b"INCEPTION-BYTES" * 400
    make_file(lib / "Videos/dump/Inception.(2010).mkv", twin)   # A: canonical
    make_file(lib / "Videos/other/Inception.(2010).mkv", twin)  # B: staged out


def test_p16_projection_is_sealed_and_beats_naive_plan(world):
    _seed_c21_unblock(world)
    cfg, st, res = analyze(world)
    secs = {s.id: s for s in res.sections}
    reorg = secs["reorganize:library"]
    # The SEALED reorganize plan is the projection: it contains the move that
    # a naive single-pass build (C21-blocked on the twin) could not have.
    assert reorg.status == "ready" and reorg.n_rows == 1
    _, rows, _ = report.read_plan(reorg.plan_path)
    assert rows[0]["src"].endswith(n("Videos", "dump", "Inception.(2010).mkv"))
    assert n("Video", "Movies", "Other") in rows[0]["dst"]
    assert any("projected end-state across convergence" in note
               for note in reorg.notes)


def test_p16_execute_projection_has_zero_convergence_delta(world):
    """With the projection sealed, Pass-2 executes it in the first cycle and
    convergence finds nothing new — the delta audit is 0 (nothing ran that
    the human didn't review)."""
    _seed_c21_unblock(world)
    cfg, st, res = analyze(world)
    proposal = read_proposal(res.proposal_path)
    approvals = pilotmod.approve_all(proposal)
    xres = execute(world, cfg, st, res.proposal_path, approvals)
    outs = outcomes_by_id(xres)
    reorg = outs["reorganize:library"]
    assert reorg.status == "converged"
    delta = sum(c.get("done", 0) for c in reorg.counts_by_cycle[1:])
    assert delta == 0, f"unexpected convergence delta: {reorg.counts_by_cycle}"
    # the projected move actually landed, the twin was staged out
    assert xres.verify["library"]["missing"] == 0
    assert (world["lib"] / "Video/Movies/Other/Inception (2010)"
            / "Inception (2010).mkv").exists()


def test_p16_rehearsal_never_touches_the_real_index(world):
    """The rehearsal runs on an in-memory copy; analyze must not mutate the
    real index or journal as a side effect of rehearsing."""
    _seed_c21_unblock(world)
    cfg, st, res = analyze(world)
    # Two files indexed, nothing moved/staged by analyze itself.
    assert st.index_count() == 2
    assert st.journal_pos() == 0
