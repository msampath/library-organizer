"""P21/C2: the ops.kind CHECK constraint widens to include 'dispose' —
SCHEMA_VERSION 1 -> 2. Existing v1 workspaces (predating this change) must
upgrade in place, keeping their data, the next time they're opened."""
from __future__ import annotations

import sqlite3

from mlo.store import Store

_V1_OPS_SCHEMA = """
CREATE TABLE ops (
  op_id TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL,
  plan_id TEXT,
  kind TEXT NOT NULL CHECK (kind IN
    ('stage_move','copy_in','move_within','rmdir_empty')),
  src BLOB NOT NULL,
  dst BLOB NOT NULL,
  src_display TEXT NOT NULL,
  dst_display TEXT NOT NULL,
  pre_size INTEGER,
  pre_quick_hash TEXT,
  state TEXT NOT NULL CHECK (state IN
    ('pending','done','skipped_done','skipped_drift','skipped_protected','failed')),
  detail TEXT NOT NULL DEFAULT '',
  committed_at TEXT NOT NULL);
CREATE INDEX idx_ops_state ON ops(state);
CREATE INDEX idx_ops_plan ON ops(plan_id);
"""


def _make_v1_db(db_path: str) -> None:
    con = sqlite3.connect(db_path)
    con.executescript(_V1_OPS_SCHEMA)
    con.execute(
        "INSERT INTO ops (op_id, run_id, plan_id, kind, src, dst, src_display,"
        " dst_display, pre_size, pre_quick_hash, state, detail, committed_at)"
        " VALUES ('op-1','run-1',NULL,'move_within',X'6162',X'6364','ab','cd',"
        " 3,'qh','done','','2026-01-01T00:00:00')")
    con.execute("PRAGMA user_version=1")
    con.commit()
    con.close()


def test_v1_database_upgrades_in_place_and_keeps_data(tmp_path):
    ws = tmp_path / ".mlo"
    ws.mkdir()
    _make_v1_db(str(ws / "state.db"))

    store = Store.open(str(ws))
    try:
        con = store._con
        assert con.execute("PRAGMA user_version").fetchone()[0] == 2
        row = con.execute(
            "SELECT run_id, kind, state FROM ops WHERE op_id='op-1'").fetchone()
        assert row == ("run-1", "move_within", "done")
    finally:
        store.close()


def test_v1_database_accepts_dispose_ops_after_upgrade(tmp_path):
    ws = tmp_path / ".mlo"
    ws.mkdir()
    _make_v1_db(str(ws / "state.db"))

    store = Store.open(str(ws))
    try:
        store.journal_intent("run-2", None, "op-dispose", "dispose",
                             "/staging/x", "/staging/x", 1, "qh")
        assert store.op_state("op-dispose") == "pending"
    finally:
        store.close()


def test_pre_versioning_legacy_db_with_user_version_zero_still_migrates(tmp_path):
    """Super-review B-016: a workspace created BEFORE the user_version stamp
    existed reads 0 — exactly like a fresh database. Stamping it straight to
    2 without the rebuild left the old CHECK constraint in place, and the
    first dispose journal_intent raised IntegrityError. The live ops DDL,
    not the stamp, must decide."""
    ws = tmp_path / ".mlo"
    ws.mkdir()
    _make_v1_db(str(ws / "state.db"))
    con = sqlite3.connect(str(ws / "state.db"))
    con.execute("PRAGMA user_version=0")        # pre-versioning workspace
    con.commit(); con.close()

    store = Store.open(str(ws))
    try:
        assert store._con.execute("PRAGMA user_version").fetchone()[0] == 2
        # data survived AND the widened constraint accepts dispose
        row = store._con.execute(
            "SELECT run_id FROM ops WHERE op_id='op-1'").fetchone()
        assert row == ("run-1",)
        store.journal_intent("run-2", None, "op-d", "dispose",
                             "/staging/x", "/staging/x", 1, "qh")
        assert store.op_state("op-d") == "pending"
    finally:
        store.close()


def test_migration_preserves_rowid_journal_positions(tmp_path):
    """Super-review B-021: rowid IS the journal position (L1) — the rebuild
    must copy it explicitly, not rely on SELECT * (which drops rowid)."""
    ws = tmp_path / ".mlo"
    ws.mkdir()
    db = str(ws / "state.db")
    _make_v1_db(db)
    con = sqlite3.connect(db)
    orig = con.execute("SELECT rowid FROM ops WHERE op_id='op-1'").fetchone()[0]
    con.close()

    store = Store.open(str(ws))
    try:
        migrated = store._con.execute(
            "SELECT rowid FROM ops WHERE op_id='op-1'").fetchone()[0]
        assert migrated == orig
    finally:
        store.close()


def test_reopening_an_already_migrated_db_is_a_no_op(tmp_path):
    ws = tmp_path / ".mlo"
    ws.mkdir()
    _make_v1_db(str(ws / "state.db"))
    Store.open(str(ws)).close()

    store = Store.open(str(ws))
    try:
        assert store._con.execute("PRAGMA user_version").fetchone()[0] == 2
        row = store._con.execute(
            "SELECT op_id FROM ops WHERE op_id='op-1'").fetchone()
        assert row is not None
    finally:
        store.close()
