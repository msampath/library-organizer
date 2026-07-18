"""P21/C5: mlo doctor — read-only health report. Closes G349: config.validate
checks library/source root existence but never staging; doctor does."""
from __future__ import annotations

import os

from helpers import make_cfg
from mlo import doctor as doctormod


def test_report_library_ok(world):
    cfg = make_cfg(world)
    rep = doctormod.report(cfg, world["store"])
    assert rep["library"]["status"] == "ok"
    assert rep["version"]


def test_report_flags_missing_source_root(world):
    from mlo.config import Source
    cfg = make_cfg(world, sources=(Source("gone", str(world["tmp"] / "nope"), True),))
    rep = doctormod.report(cfg, world["store"])
    assert rep["sources"][0]["status"] == "MISSING"


def test_report_disabled_source_is_not_flagged_missing(world):
    from mlo.config import Source
    cfg = make_cfg(world,
                   sources=(Source("gone", str(world["tmp"] / "nope"), False),))
    rep = doctormod.report(cfg, world["store"])
    assert rep["sources"][0]["status"] == "disabled"


def test_report_flags_missing_staging_root(world):
    """The gap doctor exists to close: validate() never checks staging."""
    cfg = make_cfg(world, staging={"E": str(world["tmp"] / "no-such-staging")})
    rep = doctormod.report(cfg, world["store"])
    assert rep["staging"][0]["status"] == "MISSING"


def test_report_staging_root_that_exists_is_ok(world):
    cfg = make_cfg(world)
    rep = doctormod.report(cfg, world["store"])
    assert all(s["status"] == "ok" for s in rep["staging"])


def test_report_flags_pending_journal_rows(world):
    cfg = make_cfg(world)
    store = world["store"]
    store.journal_intent("run-x", None, "op-1", "move_within",
                         str(world["lib"] / "a"), str(world["lib"] / "b"),
                         None, None)
    rep = doctormod.report(cfg, store)
    assert rep["store"]["pending_ops"] == 1


def test_report_llm_chain_none_when_disabled(world):
    cfg = make_cfg(world)
    rep = doctormod.report(cfg, world["store"])
    assert rep["llm_chain"] is None


def test_report_last_run_none_initially(world):
    cfg = make_cfg(world)
    rep = doctormod.report(cfg, world["store"])
    assert rep["last_run"] is None


def test_report_last_run_reflects_most_recent(world):
    cfg = make_cfg(world)
    store = world["store"]
    store.start_run("scan", [], cfg.config_hash, "t")
    rid2 = store.start_run("verdicts", [], cfg.config_hash, "t")
    rep = doctormod.report(cfg, store)
    assert rep["last_run"]["run_id"] == rid2
    assert rep["last_run"]["command"] == "verdicts"
