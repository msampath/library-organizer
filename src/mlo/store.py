"""The operational store: one SQLite database is the sole truth (defect L7).

Holds four things:
  runs       — the run ledger
  ops        — the append-only mutation journal (rowid IS the journal position; L1)
  files      — the library fingerprint index, maintained transactionally with ops
  source_files / artifacts — scans, verdicts, and their freshness stamps

CSV/JSON are exported *views* written by report.py; nothing in the engine reads
them back. Paths are stored as surrogatepass BLOBs (L10) with lossy *_display
columns for humans.

Concurrency model (v0.1): one writer connection per process; scans batch their
inserts; `apply` is single-threaded. WAL lets `mlo status` read concurrently.
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass

from . import winpath
from .config import ConfigError

# ONE definition of the ops columns, shared by the schema and the v1->v2
# migration rebuild — two hand-synced copies is the L6 diverging-lists class.
_OPS_COLUMNS = """
  op_id TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL,
  plan_id TEXT,
  kind TEXT NOT NULL CHECK (kind IN
    ('stage_move','copy_in','move_within','rmdir_empty','dispose')),
  src BLOB NOT NULL,
  dst BLOB NOT NULL,
  src_display TEXT NOT NULL,
  dst_display TEXT NOT NULL,
  pre_size INTEGER,
  pre_quick_hash TEXT,
  state TEXT NOT NULL CHECK (state IN
    ('pending','done','skipped_done','skipped_drift','skipped_protected','failed')),
  detail TEXT NOT NULL DEFAULT '',
  committed_at TEXT NOT NULL"""

_OPS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_ops_state ON ops(state);
CREATE INDEX IF NOT EXISTS idx_ops_plan ON ops(plan_id);
CREATE INDEX IF NOT EXISTS idx_ops_run ON ops(run_id);"""

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  command TEXT NOT NULL,
  args_json TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  code_version TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN
    ('running','completed','completed_with_residuals','failed','interrupted')));

CREATE TABLE IF NOT EXISTS ops ({_OPS_COLUMNS});
{_OPS_INDEXES}

CREATE TABLE IF NOT EXISTS files (
  file_id INTEGER PRIMARY KEY,
  relpath BLOB NOT NULL UNIQUE,
  relpath_display TEXT NOT NULL,
  size INTEGER NOT NULL,
  quick_hash TEXT NOT NULL,
  full_hash TEXT,
  mtime_ns INTEGER,
  scan_id TEXT NOT NULL);

CREATE INDEX IF NOT EXISTS idx_files_fp ON files(size, quick_hash);

CREATE TABLE IF NOT EXISTS source_files (
  id INTEGER PRIMARY KEY,
  source_name TEXT NOT NULL,
  relpath BLOB NOT NULL,
  relpath_display TEXT NOT NULL,
  size INTEGER NOT NULL,
  quick_hash TEXT NOT NULL,
  mtime_ns INTEGER,
  scan_id TEXT NOT NULL,
  verdict TEXT CHECK (verdict IN ('ORGANIZED','JUNK','UNIQUE','REVIEW') OR verdict IS NULL),
  verdict_rule TEXT,
  UNIQUE (source_name, relpath));

CREATE INDEX IF NOT EXISTS idx_source_files ON source_files(source_name, verdict);

CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  scope_json TEXT NOT NULL,
  built_at TEXT NOT NULL,
  journal_pos INTEGER NOT NULL,
  config_hash TEXT NOT NULL,
  run_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('fresh','stale','building','failed','executed')));
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    kind: str
    scope: dict
    built_at: str
    journal_pos: int
    config_hash: str
    run_id: str
    status: str


def _ops_needs_dispose_migration(con: sqlite3.Connection) -> bool:
    """True when the live ops table's DDL predates the 'dispose' kind — the
    ACTUAL signal a rebuild is needed. user_version alone cannot be trusted
    for this: a workspace created before versioning existed reads 0, exactly
    like a fresh database (super-review B-016)."""
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ops'"
    ).fetchone()
    return bool(row and row[0]) and "'dispose'" not in row[0]


def _migrate_v1_to_v2(con: sqlite3.Connection) -> None:
    """P21/C2: the `ops.kind` CHECK constraint widens to include 'dispose'.
    SQLite can't ALTER a CHECK constraint in place — rebuild the table (the
    standard SQLite shape: rename, recreate, copy, drop) inside ONE explicit
    transaction, so a crash mid-migration rolls back to the intact v1 table
    instead of stranding an ops_v1 leftover that bricks every later open
    (super-review B-017). rowid is copied EXPLICITLY: it IS the journal
    position (L1) and `SELECT *` does not carry it."""
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("ALTER TABLE ops RENAME TO ops_v1")
        con.execute(f"CREATE TABLE ops ({_OPS_COLUMNS})")
        con.execute("INSERT INTO ops (rowid, op_id, run_id, plan_id, kind,"
                    " src, dst, src_display, dst_display, pre_size,"
                    " pre_quick_hash, state, detail, committed_at)"
                    " SELECT rowid, op_id, run_id, plan_id, kind,"
                    " src, dst, src_display, dst_display, pre_size,"
                    " pre_quick_hash, state, detail, committed_at"
                    " FROM ops_v1 ORDER BY rowid")
        con.execute("DROP TABLE ops_v1")
        for stmt in _OPS_INDEXES.strip().split(";"):
            if stmt.strip():
                con.execute(stmt)
        con.execute(f"PRAGMA user_version={Store.SCHEMA_VERSION}")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


class Store:
    """Open with Store.open(workspace_dir); one instance per process."""

    def __init__(self, con: sqlite3.Connection, workspace: str):
        self._con = con
        self.workspace = workspace

    # ── lifecycle ─────────────────────────────────────────────────────────

    SCHEMA_VERSION = 2

    @classmethod
    def open(cls, workspace_dir: str) -> "Store":
        os.makedirs(workspace_dir, exist_ok=True)
        db_path = os.path.join(workspace_dir, "state.db")
        con = sqlite3.connect(db_path)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=FULL")
            # Two mlo processes may share a workspace (e.g. a scan running
            # while status/report reads); WAL allows that, but a brief
            # write-write overlap must wait, not hard-fail with "database is
            # locked".
            con.execute("PRAGMA busy_timeout=30000")
            con.executescript(_SCHEMA)
            # Schema version stamp: a schema change bumps SCHEMA_VERSION and
            # migrates stepwise; older code refuses a newer db instead of
            # silently misreading it (L7 — the store is the sole truth and
            # must survive upgrades).
            ver = con.execute("PRAGMA user_version").fetchone()[0]
            if ver > cls.SCHEMA_VERSION:
                con.close()
                raise ConfigError(
                    f"{db_path} has schema version {ver}, newer than this "
                    f"mlo understands ({cls.SCHEMA_VERSION}) — upgrade mlo")
            # ver==0 is ambiguous: a fresh database OR a workspace created
            # before versioning existed (whose old ops table lacks the
            # 'dispose' kind). The live DDL, not the stamp, decides
            # (super-review B-016).
            if ver <= 1 and _ops_needs_dispose_migration(con):
                _migrate_v1_to_v2(con)      # stamps SCHEMA_VERSION itself
            elif ver < cls.SCHEMA_VERSION:
                con.execute(f"PRAGMA user_version={cls.SCHEMA_VERSION}")
            con.commit()
        except sqlite3.DatabaseError as e:
            con.close()
            bdir = os.path.join(workspace_dir, "backups")
            snaps = sorted(glob.glob(os.path.join(bdir, "state-*.db")))
            hint = (f"; newest snapshot: {snaps[-1]}" if snaps
                    else "; no snapshots found")
            raise ConfigError(
                f"cannot open {db_path}: {e} (corrupt or not a database) — "
                f"restore from {bdir}{hint}") from e
        return cls(con, workspace_dir)

    def snapshot(self) -> str:
        """VACUUM INTO backup at run start; returns the snapshot path. Two
        runs starting within the same second must not share a snapshot slot —
        the second would silently get the FIRST run's (pre-mutation) copy
        back as if freshly taken (super-review B-024)."""
        bdir = os.path.join(self.workspace, "backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(bdir, f"state-{stamp}.db")
        n = 1
        while os.path.exists(dest):
            dest = os.path.join(bdir, f"state-{stamp}-{n}.db")
            n += 1
        self._con.execute("VACUUM INTO ?", (dest,))
        return dest

    def copy_for_rehearsal(self) -> "Store":
        """A THROWAWAY IN-MEMORY copy of this store, for Pass-1 convergence
        rehearsal (pilot). The whole db is copied into a `:memory:` connection
        via sqlite's backup API; the caller mutates ONLY the copy's index via
        `simulate_apply`, then closes it — nothing is written to disk and
        nothing is ever deleted (mlo creates no deletion primitives, L0/L18).
        The copy keeps this store's `workspace` so the rehearsal's builders
        write their (content-addressed, harmless) plan files to the normal
        plans dir exactly as the naive section builds already do. This NEVER
        touches this store's connection, journal, or the filesystem library —
        the rehearsal's inviolable safety boundary."""
        mem = sqlite3.connect(":memory:")
        self._con.backup(mem)          # full schema + data + user_version
        mem.execute("PRAGMA busy_timeout=30000")
        return Store(mem, self.workspace)

    def simulate_apply(self, rows: list[dict], library_root: str) -> None:
        """Apply ONLY the library-index EFFECT of plan rows to THIS store's
        index — no journal, no filesystem, no fingerprinting. Used by the
        pilot's Pass-1 rehearsal on a scratch copy so the next builder sees
        the post-move index. Mirrors the kernel's success-path effects:
        `move_within` -> index move; `stage_move` -> index delete (the file
        left the library); `rmdir_empty`/`copy_in` -> no effect here (the
        rehearsed movers never emit them). MUST only ever be called on a
        `copy_for_rehearsal` scratch store, never the real one."""
        for r in rows:
            kind = r["kind"]
            try:
                src_rel = os.path.relpath(r["src"], library_root)
            except ValueError:
                continue                       # different drive — not a member
            if kind == "move_within":
                dst_rel = os.path.relpath(r["dst"], library_root)
                self._con.execute(
                    "UPDATE OR REPLACE files SET relpath=?, relpath_display=?"
                    " WHERE relpath=?",
                    (winpath.to_bytes(dst_rel), winpath.display(dst_rel),
                     winpath.to_bytes(src_rel)))
            elif kind == "stage_move":
                self._con.execute("DELETE FROM files WHERE relpath=?",
                                  (winpath.to_bytes(src_rel),))
        self._con.commit()

    def close(self) -> None:
        self._con.close()

    # ── runs ──────────────────────────────────────────────────────────────

    def start_run(self, command: str, args: list[str], config_hash: str,
                  code_version: str) -> str:
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self._con.execute(
            "INSERT INTO runs (run_id, command, args_json, config_hash,"
            " code_version, started_at, finished_at, status)"
            " VALUES (?,?,?,?,?,?,NULL,'running')",
            (run_id, command, json.dumps(args), config_hash, code_version, _now()))
        self._con.commit()
        return run_id

    def finish_run(self, run_id: str, status: str) -> None:
        self._con.execute(
            "UPDATE runs SET finished_at=?, status=? WHERE run_id=?",
            (_now(), status, run_id))
        self._con.commit()

    def get_run(self, run_id: str) -> dict | None:
        cur = self._con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    # ── journal ───────────────────────────────────────────────────────────

    def journal_pos(self) -> int:
        row = self._con.execute("SELECT COALESCE(MAX(rowid),0) FROM ops").fetchone()
        return int(row[0])

    def op_state(self, op_id: str) -> str | None:
        row = self._con.execute(
            "SELECT state FROM ops WHERE op_id=?", (op_id,)).fetchone()
        return row[0] if row else None

    def journal_intent(self, run_id: str, plan_id: str | None, op_id: str,
                       kind: str, src: str, dst: str,
                       pre_size: int | None, pre_quick_hash: str | None) -> None:
        """Write (and COMMIT) the 'pending' intent before the kernel acts, so a
        crash between the syscall and the done-mark leaves a durable pending row
        the reconciler can resolve (defect L2/architecture §4). One row per
        op_id for its whole life; only a non-'done' op re-enters — a re-derived
        drift/protected/failed skip can retry once its cause is fixed (defect
        L1/L5). A 'done' op never re-pends (the kernel's gate stops it earlier),
        and plan_id is refreshed so retried ops attribute to the current run."""
        self._con.execute(
            "INSERT INTO ops (op_id, run_id, plan_id, kind, src, dst, src_display,"
            " dst_display, pre_size, pre_quick_hash, state, detail, committed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,'pending','',?)"
            " ON CONFLICT(op_id) DO UPDATE SET"
            "  state='pending', run_id=excluded.run_id, plan_id=excluded.plan_id,"
            "  detail='retry', committed_at=excluded.committed_at,"
            # pre fields refresh on retry: a drift/protected skip journals
            # pre as NULL, and a stale NULL pre silently disables the L2
            # crash-reconcile hash check on the retried op.
            "  pre_size=excluded.pre_size,"
            "  pre_quick_hash=excluded.pre_quick_hash"
            " WHERE ops.state != 'done'",
            (op_id, run_id, plan_id, kind,
             winpath.to_bytes(src), winpath.to_bytes(dst),
             winpath.display(src), winpath.display(dst),
             pre_size, pre_quick_hash, _now()))
        self._con.commit()

    def complete_op(self, op_id: str, state: str, detail: str = "",
                    index_effect: tuple | None = None,
                    library_root: str | None = None) -> None:
        """Terminal-state an op and apply its library-index effect in the SAME
        transaction (defect L7: the index cannot go stale from engine actions).

        index_effect: ('insert', relpath, size, quick_hash, mtime_ns, scan_id)
                    | ('move', old_relpath, new_relpath)
                    | ('delete', relpath)
                    | None
        """
        # `AND state != 'done'` mirrors journal_intent's re-pend guard: a
        # terminal 'done' is immutable — a racing second writer (the web UI's
        # request threads) must never demote a completed op to 'failed'.
        self._con.execute(
            "UPDATE ops SET state=?, detail=?, committed_at=?"
            " WHERE op_id=? AND state != 'done'",
            (state, detail, _now(), op_id))
        if index_effect is not None:
            eff = index_effect[0]
            if eff == "insert":
                _, rel, size, qh, mtime_ns, scan_id = index_effect
                self._con.execute(
                    "INSERT OR REPLACE INTO files"
                    " (relpath, relpath_display, size, quick_hash, mtime_ns, scan_id)"
                    " VALUES (?,?,?,?,?,?)",
                    (winpath.to_bytes(rel), winpath.display(rel), size, qh,
                     mtime_ns, scan_id))
            elif eff == "move":
                _, old_rel, new_rel = index_effect
                # UPDATE OR REPLACE: the kernel verified the destination
                # state before acting; if a stale row already sits at
                # new_rel (e.g. a crash-reconciled duplicate), a plain
                # UPDATE would raise UNIQUE and strand the op 'pending'
                # with the disk already changed.
                self._con.execute(
                    "UPDATE OR REPLACE files SET relpath=?, relpath_display=?"
                    " WHERE relpath=?",
                    (winpath.to_bytes(new_rel), winpath.display(new_rel),
                     winpath.to_bytes(old_rel)))
            elif eff == "delete":
                _, rel = index_effect
                self._con.execute("DELETE FROM files WHERE relpath=?",
                                  (winpath.to_bytes(rel),))
            else:  # pragma: no cover — programming error
                raise ValueError(f"unknown index effect {eff!r}")
        if state == "done":
            self._flip_stale_for_op(op_id)
        self._con.commit()

    def pending_ops(self) -> list[dict]:
        cur = self._con.execute(
            "SELECT op_id, kind, src, dst, pre_size, pre_quick_hash, plan_id"
            " FROM ops WHERE state='pending' ORDER BY rowid")
        return [
            {"op_id": r[0], "kind": r[1], "src": winpath.from_bytes(r[2]),
             "dst": winpath.from_bytes(r[3]), "pre_size": r[4],
             "pre_quick_hash": r[5], "plan_id": r[6]}
            for r in cur.fetchall()]

    def mark_retryable(self, op_id: str, detail: str,
                       drop_index_relpath: str | None = None) -> None:
        """Reset a 'done' op whose post-conditions the audit found unmet, so its
        residual row can retry with the same op_id (defect L5). Only touches a
        'done' row. If the op had inserted a library-index row (a copy/move INTO
        the library) that is now bogus, reverse it in the SAME transaction —
        otherwise a phantom index row survives and drives a false ORGANIZED
        verdict (defect C7, found in 2nd-order review)."""
        self._con.execute(
            "UPDATE ops SET state='failed', detail=?, committed_at=?"
            " WHERE op_id=? AND state='done'",
            (detail, _now(), op_id))
        if drop_index_relpath is not None:
            self._con.execute("DELETE FROM files WHERE relpath=?",
                              (winpath.to_bytes(drop_index_relpath),))
        self._con.commit()

    def staged_dsts(self) -> set[str]:
        """Lossless destination paths of every completed staging/copy/move op —
        for verify_staging to recognize journaled content even when the name
        carries lone surrogates (defect L10: display strings are lossy)."""
        cur = self._con.execute(
            "SELECT dst FROM ops WHERE state='done'"
            " AND kind IN ('stage_move','copy_in','move_within')")
        return {os.path.normcase(winpath.from_bytes(r[0])) for r in cur.fetchall()}

    def staged_dst_fingerprints(self) -> dict[str, tuple[int | None, str | None]]:
        """normcased+normpathed dst -> (pre_size, pre_quick_hash) of the most
        recent DONE staging op that placed it — build_dispose's content check
        (P21/C68): a dispose plan may only claim a staged file whose CURRENT
        bytes still match what the engine journaled staging there; replaced
        content at a journaled path must never be disposed blind."""
        cur = self._con.execute(
            "SELECT dst, pre_size, pre_quick_hash FROM ops WHERE state='done'"
            " AND kind='stage_move' ORDER BY rowid")
        out: dict[str, tuple[int | None, str | None]] = {}
        for r in cur:            # journal order: the latest op wins per path
            key = os.path.normpath(os.path.normcase(winpath.from_bytes(r[0])))
            out[key] = (r[1], r[2])
        return out

    def origin_pairs(self, kinds: tuple[str, ...] = ("copy_in", "move_within")):
        """(dst, src) of every DONE placement op, in journal order — the raw
        material for provenance tracing (current library path ↔ where it came
        from). Later ops win when a path is reused, so a caller building a map
        in this order ends with the most recent origin for each dst."""
        ph = ",".join("?" * len(kinds))
        cur = self._con.execute(
            f"SELECT dst, src FROM ops WHERE state='done' AND kind IN ({ph})"
            " ORDER BY rowid", kinds)
        for r in cur:
            yield winpath.from_bytes(r[0]), winpath.from_bytes(r[1])

    def export_ops(self, run_id: str | None = None):
        """Op rows as dicts. Carries BOTH the lossy *_display strings (for
        CSV views) and the LOSSLESS src/dst decoded from the BLOB columns
        (L10) — consumers that act on paths (undo) must use src/dst, never
        the display strings, or surrogate-named files become unreachable."""
        q = ("SELECT rowid, op_id, run_id, plan_id, kind, src, dst,"
             " src_display, dst_display,"
             " pre_size, pre_quick_hash, state, detail, committed_at FROM ops")
        args: tuple = ()
        if run_id:
            q += " WHERE run_id=?"
            args = (run_id,)
        cur = self._con.execute(q + " ORDER BY rowid", args)
        cols = [d[0] for d in cur.description]
        for row in cur:
            d = dict(zip(cols, row))
            d["src"] = winpath.from_bytes(d["src"])
            d["dst"] = winpath.from_bytes(d["dst"])
            yield d

    # ── library index ─────────────────────────────────────────────────────

    def index_upsert(self, relpath: str, size: int, quick_hash: str,
                     mtime_ns: int, scan_id: str) -> None:
        self._con.execute(
            "INSERT OR REPLACE INTO files"
            " (relpath, relpath_display, size, quick_hash, mtime_ns, scan_id)"
            " VALUES (?,?,?,?,?,?)",
            (winpath.to_bytes(relpath), winpath.display(relpath), size,
             quick_hash, mtime_ns, scan_id))

    def index_commit(self) -> None:
        self._con.commit()

    def index_lookup(self, size: int, quick_hash: str) -> list[str]:
        cur = self._con.execute(
            "SELECT relpath FROM files WHERE size=? AND quick_hash=?",
            (size, quick_hash))
        return [winpath.from_bytes(r[0]) for r in cur.fetchall()]

    def index_get(self, relpath: str) -> dict | None:
        cur = self._con.execute(
            "SELECT size, quick_hash, mtime_ns FROM files WHERE relpath=?",
            (winpath.to_bytes(relpath),))
        r = cur.fetchone()
        return {"size": r[0], "quick_hash": r[1], "mtime_ns": r[2]} if r else None

    def index_count(self) -> int:
        return int(self._con.execute("SELECT COUNT(*) FROM files").fetchone()[0])

    def index_iter(self):
        cur = self._con.execute(
            "SELECT relpath, size, quick_hash, mtime_ns FROM files")
        for r in cur:
            yield {"relpath": winpath.from_bytes(r[0]), "size": r[1],
                   "quick_hash": r[2], "mtime_ns": r[3]}

    # ── source files / verdicts ───────────────────────────────────────────

    def source_upsert(self, source_name: str, relpath: str, size: int,
                      quick_hash: str, mtime_ns: int, scan_id: str) -> None:
        self._con.execute(
            "INSERT INTO source_files"
            " (source_name, relpath, relpath_display, size, quick_hash, mtime_ns,"
            "  scan_id, verdict, verdict_rule)"
            " VALUES (?,?,?,?,?,?,?,NULL,NULL)"
            " ON CONFLICT(source_name, relpath) DO UPDATE SET"
            "  size=excluded.size, quick_hash=excluded.quick_hash,"
            "  mtime_ns=excluded.mtime_ns, scan_id=excluded.scan_id,"
            "  verdict=NULL, verdict_rule=NULL",
            (source_name, winpath.to_bytes(relpath), winpath.display(relpath),
             size, quick_hash, mtime_ns, scan_id))


    def source_delete_not_in_scan(self, source_name: str, scan_id: str) -> int:
        cur = self._con.execute(
            "DELETE FROM source_files WHERE source_name=? AND scan_id != ?",
            (source_name, scan_id))
        self._con.commit()
        return cur.rowcount

    def source_set_verdict(self, source_name: str, relpath: str,
                           verdict: str, rule: str) -> None:
        self._con.execute(
            "UPDATE source_files SET verdict=?, verdict_rule=?"
            " WHERE source_name=? AND relpath=?",
            (verdict, rule, source_name, winpath.to_bytes(relpath)))

    def source_iter(self, source_name: str, verdict: str | None = None):
        q = ("SELECT relpath, size, quick_hash, mtime_ns, verdict, verdict_rule"
             " FROM source_files WHERE source_name=?")
        args: list = [source_name]
        if verdict is not None:
            q += " AND verdict=?"
            args.append(verdict)
        cur = self._con.execute(q, args)
        for r in cur:
            yield {"relpath": winpath.from_bytes(r[0]), "size": r[1],
                   "quick_hash": r[2], "mtime_ns": r[3], "verdict": r[4],
                   "verdict_rule": r[5]}

    def source_verdict_counts(self, source_name: str) -> dict[str, int]:
        cur = self._con.execute(
            "SELECT COALESCE(verdict,'(none)'), COUNT(*) FROM source_files"
            " WHERE source_name=? GROUP BY verdict", (source_name,))
        return {r[0]: r[1] for r in cur.fetchall()}

    def source_commit(self) -> None:
        self._con.commit()

    # ── artifacts / freshness (defect L7) ─────────────────────────────────

    def artifact_register(self, artifact_id: str, kind: str, scope: dict,
                          config_hash: str, run_id: str,
                          status: str = "fresh") -> None:
        """Register/refresh an artifact. An 'executed' plan artifact is NEVER
        silently reverted to 'fresh' by an identical rebuild (defect L13: that
        would erase execution history and falsely re-block the dedup gate)."""
        existing = self.artifact_get(artifact_id)
        if (existing is not None and existing.status == "executed"
                and status == "fresh"):
            return
        self._con.execute(
            "INSERT OR REPLACE INTO artifacts (artifact_id, kind, scope_json,"
            " built_at, journal_pos, config_hash, run_id, status)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (artifact_id, kind, json.dumps(scope), _now(), self.journal_pos(),
             config_hash, run_id, status))
        self._con.commit()

    def artifact_get(self, artifact_id: str) -> Artifact | None:
        row = self._con.execute(
            "SELECT artifact_id, kind, scope_json, built_at, journal_pos,"
            " config_hash, run_id, status FROM artifacts WHERE artifact_id=?",
            (artifact_id,)).fetchone()
        if not row:
            return None
        return Artifact(row[0], row[1], json.loads(row[2]), row[3], row[4],
                        row[5], row[6], row[7])

    def artifact_set_status(self, artifact_id: str, status: str) -> None:
        self._con.execute(
            "UPDATE artifacts SET status=? WHERE artifact_id=?",
            (status, artifact_id))
        self._con.commit()

    def artifact_fresh(self, artifact_id: str, config_hash: str) -> bool:
        a = self.artifact_get(artifact_id)
        return (a is not None and a.status == "fresh"
                and a.config_hash == config_hash)

    def last_run(self) -> dict | None:
        """The most recently STARTED run (by started_at), for `mlo doctor`
        (P21/C5) — a quick 'what did this workspace last do' answer."""
        cur = self._con.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def artifacts_all(self) -> list[Artifact]:
        cur = self._con.execute(
            "SELECT artifact_id, kind, scope_json, built_at, journal_pos,"
            " config_hash, run_id, status FROM artifacts ORDER BY artifact_id")
        return [Artifact(r[0], r[1], json.loads(r[2]), r[3], r[4], r[5], r[6], r[7])
                for r in cur.fetchall()]

    def _flip_stale_for_op(self, op_id: str) -> None:
        """A done op makes scan/verdict artifacts stale when their scoped CONTENT
        changed — i.e. an end the op MUTATED lies inside the scope. copy_in only
        reads its source, so it does not stale the source's artifacts (organizing
        a source must not invalidate that source's own dedup plan). The library
        index artifact never flips: it is maintained in the same transaction as
        the op (architecture §6)."""
        row = self._con.execute(
            "SELECT kind, src_display, dst_display FROM ops WHERE op_id=?",
            (op_id,)).fetchone()
        if not row:
            return
        kind, src, dst = row
        mutated = [dst] if kind == "copy_in" else [src, dst]
        for a in self.artifacts_all():
            if a.kind not in ("scan", "verdicts") or a.status != "fresh":
                continue
            root = a.scope.get("root", "")
            if root and any(winpath.is_under(p, root) for p in mutated):
                self._con.execute(
                    "UPDATE artifacts SET status='stale' WHERE artifact_id=?",
                    (a.artifact_id,))
