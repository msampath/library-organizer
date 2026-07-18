"""The web UI (`mlo serve`): a localhost-only front for the SAME safe engine
the CLI drives. This module NEVER touches the filesystem itself — it calls
scan/verdict/plan/apply/verify/pilot and lets the kernel own every mutation
(the architecture test enforces this by walking every module's AST). The only
writes it causes are through whitelisted report helpers (generated config,
approvals audit, run artifacts).

Two flows on one page:

* PRIMARY — the 2-pass product. "Analyze" runs `pilot.analyze` (Pass 1:
  read-only + rehearsed, one sealed proposal) on a background job thread; the
  review screen renders the proposal's sections/clusters/rows with the full
  review signals and critic answers; "Execute" persists the approvals (audit
  first), then runs `pilot.execute` (Pass 2) hash-bound to the reviewed
  proposal (approve-X-execute-X, ledger C25).
* SECONDARY — the original guided stepper (pick two folders, approve each
  interim output) kept intact as "Guided mode".

Jobs: ONE background worker at a time (`_JOB` + lock). The worker opens ITS
OWN Store (sqlite connections are per-thread; the same pattern as the
per-request `_cfg_store`) and closes it in `finally`. Engine refusals surface
verbatim as banners (`_EXPECTED`); unexpected errors surface with their real
message — success is never fabricated.

Bind is 127.0.0.1 only: the execute endpoints move files, so they must never
be reachable off the machine.
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import __version__, apply as applymod, plan as planmod, report, scan
from . import pilot as pilotmod
from . import verdict, winpath
from .agent.llm import LLMDisabled
from .config import Config, ConfigError, load, validate
from .store import Store
from .verdict import StaleArtifactError
from .verify import verify_library

# Engine refusals the UI should render as a remedy, not a crash.
_EXPECTED = (ConfigError, LLMDisabled, planmod.PlanError, planmod.OrderingError,
             planmod.CoverageBlockedError, report.PlanIntegrityError,
             StaleArtifactError, pilotmod.ApprovalsError)


def _workspace(cfg_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(cfg_path)), ".mlo")


def _disp(s: str) -> str:
    """JSON-safe display of a path that may carry lone surrogates."""
    return s.encode("utf-8", "replace").decode("utf-8")


def _disp_deep(v):
    """_disp applied through a nested structure — for DISPLAY-ONLY blocks
    (paths, samples, signals). Never applied to actionable identifiers
    (section/cluster/op ids, the proposal seal), which must round-trip
    losslessly through the browser and back into approvals."""
    if isinstance(v, str):
        return _disp(v)
    if isinstance(v, list):
        return [_disp_deep(x) for x in v]
    if isinstance(v, dict):
        return {k: _disp_deep(x) for k, x in v.items()}
    return v


def _cfg_store(cfg_path: str) -> tuple[Config, Store]:
    cfg = load(cfg_path)
    validate(cfg, _workspace(cfg_path))
    return cfg, Store.open(_workspace(cfg_path))


def _boot_pending_warning(cfg_path: str) -> int | None:
    """P21/C6: the same crash-recovery detection `mlo check`/`status`/
    `doctor` do, run once at `serve()` boot instead of only surfacing
    silently inside the next Execute's reconcile_pending call. Returns the
    pending count (0 if none, None if the config itself can't be opened —
    that error surfaces normally once the UI loads)."""
    try:
        cfg, store = _cfg_store(cfg_path)
    except _EXPECTED:
        return None
    try:
        n = len(store.pending_ops())
        if n:
            print(f"warning: {n} pending journal row(s) from a prior "
                  f"crash — the next Execute reconciles them automatically "
                  f"(or run: mlo doctor)", file=sys.stderr)
        return n
    finally:
        store.close()


def _source_name(cfg: Config) -> str:
    if not cfg.sources:
        raise ConfigError("config has no source — re-run setup")
    return cfg.sources[0].name


# ── the pipeline steps, each a pure action returning a JSON-able dict ──────────

def act_state(cfg_path: str) -> dict:
    st: dict = {"config_exists": os.path.exists(cfg_path),
                "config_path": os.path.abspath(cfg_path)}
    if not st["config_exists"]:
        return st
    try:
        cfg = load(cfg_path)
    except ConfigError as e:
        st["config_error"] = str(e)
        return st
    st["library_root"] = cfg.library_root
    if cfg.sources:
        st["source_name"] = cfg.sources[0].name
        st["source_root"] = cfg.sources[0].root
    store = Store.open(_workspace(cfg_path))
    try:
        name = cfg.sources[0].name if cfg.sources else None
        st["index_fresh"] = store.artifact_fresh("index:library", cfg.config_hash)
        st["source_scan_fresh"] = bool(
            name and store.artifact_fresh(f"scan:{name}", cfg.config_hash))
        st["verdicts_fresh"] = bool(
            name and store.artifact_fresh(f"verdicts:{name}", cfg.config_hash))
        st["index_count"] = store.index_count()
        # 2-pass dashboard state: journal position, per-source freshness,
        # staging roots, the newest proposal/run (paths are display-only).
        st["journal_pos"] = store.journal_pos()
        st["sources"] = [{
            "name": s.name, "root": _disp(s.root), "enabled": s.enabled,
            "scan_fresh": store.artifact_fresh(f"scan:{s.name}", cfg.config_hash),
            "verdicts_fresh": store.artifact_fresh(f"verdicts:{s.name}",
                                                   cfg.config_hash),
        } for s in cfg.sources]
    finally:
        store.close()
    st["staging"] = {d: _disp(r) for d, r in sorted(cfg.staging.items())}
    st["llm_enabled"] = cfg.llm.enabled
    ppath = _proposal_file(_workspace(cfg_path))
    st["proposal"] = ({"run": os.path.basename(os.path.dirname(ppath)),
                       "path": _disp(ppath)} if ppath else None)
    runs_dir = os.path.join(_workspace(cfg_path), "runs")
    run_ids = sorted(os.listdir(runs_dir), reverse=True) \
        if os.path.isdir(runs_dir) else []
    st["latest_run"] = run_ids[0] if run_ids else None
    st["ok"] = True
    return st


def act_setup(cfg_path: str, library_root: str, source_name: str,
              source_root: str) -> dict:
    for label, p in (("library", library_root), ("mess (source)", source_root)):
        if not p or not os.path.isdir(winpath.to_long(p)):
            return {"ok": False, "error": f"{label} folder not found: {p or '(empty)'}"}
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            if report.GENERATED_MARKER not in f.read():
                return {"ok": False, "error":
                        f"a hand-authored {os.path.abspath(cfg_path)} already "
                        f"exists — edit it directly or remove it first"}
    name = report._safe_name(source_name) if source_name else "source"
    report.write_generated_config(cfg_path, os.path.abspath(library_root), name,
                                  os.path.abspath(source_root))
    cfg = load(cfg_path)
    validate(cfg, _workspace(cfg_path))     # raises ConfigError -> handled upstream
    return {"ok": True, "library_root": cfg.library_root, "source_name": name,
            "source_root": cfg.source(name).root}


def _run(cfg: Config, store: Store, cmd: str, argv: list[str], fn):
    run_id = store.start_run(cmd, argv, cfg.config_hash, __version__)
    status = "completed"
    try:
        return fn(run_id)
    except BaseException:
        status = "failed"
        raise
    finally:
        store.finish_run(run_id, status)


def act_scan(cfg_path: str, target: str) -> dict:
    cfg, store = _cfg_store(cfg_path)
    try:
        if target == "library":
            n, skipped = _run(cfg, store, "scan library", ["scan", "library"],
                              lambda r: scan.scan_library(store, cfg, r))
            count = store.index_count()
        else:
            name = _source_name(cfg)
            n, skipped = _run(cfg, store, "scan source", ["scan", name],
                              lambda r: scan.scan_source(store, cfg, name, r))
            count = n
        return {"ok": True, "target": target, "count": count,
                "hashed": n, "unreadable": len(skipped)}
    finally:
        store.close()


def act_verdicts(cfg_path: str) -> dict:
    cfg, store = _cfg_store(cfg_path)
    try:
        name = _source_name(cfg)
        counts = _run(cfg, store, "verdicts", ["verdicts", name],
                      lambda r: verdict.assign(store, cfg, name, r))
        return {"ok": True, "counts": counts, "total": sum(counts.values())}
    finally:
        store.close()


def _folder_tree(dsts: list[str], library_root: str, max_children: int = 40) -> dict:
    """Aggregate plan destinations into a folder tree with per-branch file counts
    — the 'framework of the final structure' the user approves. Files are counted
    per folder; folders sort by size; wide folders are capped with a '+N more'."""
    root = {"name": os.path.basename(library_root.rstrip("\\/")) or library_root,
            "files": 0, "dirs": {}}
    for dst in dsts:
        rel = _disp(os.path.relpath(dst, library_root))
        parts = [p for p in rel.replace("\\", "/").split("/") if p and p != ".."]
        node = root
        for d in parts[:-1]:
            node = node["dirs"].setdefault(d, {"name": d, "files": 0, "dirs": {}})
        node["files"] += 1

    def finalize(node: dict) -> dict:
        dirs = sorted((finalize(c) for c in node["dirs"].values()),
                      key=lambda d: (-d["count"], d["name"].lower()))
        total = node["files"] + sum(d["count"] for d in dirs)
        out: dict = {"name": _disp(node["name"]), "count": total}
        more = max(0, len(dirs) - max_children)
        out["dirs"] = dirs[:max_children]
        if node["files"]:
            out["files"] = node["files"]
        if more:
            out["more"] = more
        return out

    return finalize(root)


def act_plan(cfg_path: str) -> dict:
    cfg, store = _cfg_store(cfg_path)
    try:
        name = _source_name(cfg)
        res = _run(cfg, store, "plan organize", ["plan", "organize", name],
                   lambda r: planmod.build_organize(store, cfg, name))
        _, rows, _ = report.read_plan(res.path)
        return {"ok": True, "plan_path": res.path, "plan_id": res.plan_id,
                "n_rows": res.n_rows, "notes": res.notes,
                "tree": _folder_tree([row["dst"] for row in rows],
                                     cfg.library_root),
                "sample": [{"src": _disp(row["src"]), "dst": _disp(row["dst"])}
                           for row in rows[:30]]}
    finally:
        store.close()


def act_apply(cfg_path: str, plan_path: str, execute: bool) -> dict:
    cfg, store = _cfg_store(cfg_path)
    try:
        argv = ["apply", plan_path] + (["--execute"] if execute else [])

        def go(run_id):
            if execute:
                store.snapshot()
            return applymod.apply_plan(store, cfg, plan_path, run_id,
                                       execute=execute)
        res = _run(cfg, store, "apply", argv, go)
        return {"ok": True, "execute": execute, "counts": res.counts,
                "exit_code": res.exit_code, "residual": bool(res.residual_plan),
                "drift": [{"src": _disp(d["src"]), "detail": d["detail"]}
                          for d in res.drift[:20]]}
    finally:
        store.close()


def act_verify(cfg_path: str) -> dict:
    cfg, store = _cfg_store(cfg_path)
    try:
        f = _run(cfg, store, "verify library", ["verify", "library"],
                 lambda r: verify_library(store, cfg, quick=True))
        return {"ok": True, "counts": f.counts(), "blocking": f.blocking}
    finally:
        store.close()


# ── the job runner: ONE background worker, one job at a time ──────────────────
# pilot.analyze scans + rehearses for minutes on a real library; an HTTP
# request cannot block that long. The worker owns its whole engine session
# (config load, Store open/close, run ledger) and streams progress into the
# shared _JOB view the status route serves.

_JOB_LOCK = threading.Lock()
_JOB: dict | None = None

# Test seam, mirroring the engine-wide injectable drive_of (architecture.md §3:
# "drive_of() is injectable so same-drive rules are testable on tmp dirs").
# The HTTP surface cannot inject a callable, so the seam lives here; None in
# production means the builders use the real winpath.drive_of.
_PILOT_DRIVE_OF = None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _job_start(kind: str, cfg_path: str, work) -> dict:
    """Admit ONE job. `work(cfg, store, progress)` runs on the worker thread
    with its OWN Store and returns the JSON-able job result. A second
    submission while one runs is refused, never queued (the engine is a
    single-mutator design — architecture.md §13).

    `_ACTION_LOCK` is the ONE kernel-mutex shared with the synchronous
    mutating routes: the job acquires it here (on the request thread) and the
    worker releases it when done, so a pilot job and an `/api/apply` can never
    run two kernels in one process — in EITHER direction (the reverse gap a
    review found: a job admitted while a sync action held the lock)."""
    global _JOB
    with _JOB_LOCK:
        if _JOB is not None and not _JOB["finished"]:
            return {"ok": False, "error": "a job is already running"}
        if not _ACTION_LOCK.acquire(blocking=False):
            return {"ok": False, "error": "an operation is already running"}
        job = {"job_id": f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
               "kind": kind, "phase": "starting", "events": [],
               "started": _now(), "finished": None, "error": None,
               "result": None}
        _JOB = job
    try:
        threading.Thread(target=_job_worker, args=(job, cfg_path, work),
                         name="mlo-web-job", daemon=True).start()
    except BaseException as e:
        # A start() failure would otherwise strand _ACTION_LOCK held forever
        # (every later mutator refused until restart) — release and surface.
        job["finished"] = _now()
        job["error"] = f"could not start worker thread: {e}"
        _ACTION_LOCK.release()
        raise
    return {"ok": True, "job_id": job["job_id"], "kind": kind}


def _job_worker(job: dict, cfg_path: str, work) -> None:
    def progress(phase: str, info: dict) -> None:
        with _JOB_LOCK:
            job["phase"] = phase
            job["events"].append({"phase": phase, "info": dict(info),
                                  "t": round(time.time(), 3)})

    try:
        cfg, store = _cfg_store(cfg_path)   # per-thread sqlite, like _cfg_store
        try:                                # does for every request thread
            result = work(cfg, store, progress)
        finally:
            store.close()
        with _JOB_LOCK:
            job["result"] = result
            job["phase"] = "finished"
    except _EXPECTED as e:      # engine refusal -> the remedy text, verbatim
        with _JOB_LOCK:
            job["error"] = str(e)
            job["phase"] = "error"
    except Exception as e:      # unexpected — surface the real error
        with _JOB_LOCK:
            job["error"] = f"{type(e).__name__}: {e}"
            job["phase"] = "error"
    finally:
        with _JOB_LOCK:
            job["finished"] = _now()
        _ACTION_LOCK.release()      # hand back the shared kernel-mutex


def act_pilot_status() -> dict:
    """The _JOB view (both kinds). Events are copied under the lock; entries
    are append-only and never mutated after append, so a shallow copy is a
    consistent snapshot."""
    with _JOB_LOCK:
        if _JOB is None:
            return {"ok": True, "job": None}
        j = {k: v for k, v in _JOB.items() if k != "events"}
        j["events"] = list(_JOB["events"])
        return {"ok": True, "job": j}


# ── Pass 1: analyze ────────────────────────────────────────────────────────────

def act_pilot_analyze(cfg_path: str, body: dict) -> dict:
    sources = body.get("sources") or None
    if sources is not None and (not isinstance(sources, list) or
                                not all(isinstance(s, str) for s in sources)):
        return {"ok": False, "error": "sources must be a list of source names"}
    under = body.get("under") or []
    if not isinstance(under, list) or not all(isinstance(u, str) for u in under):
        return {"ok": False, "error": "under must be a list of library prefixes"}
    def _int_field(key: str, default: int) -> int:
        # `or default` would coerce an EXPLICIT 0 to the default — and 0 is
        # meaningful for both fields (confirm_mb=0 disables the confirm pass,
        # critic_limit=0 disables the critics), exactly as the CLI honors it.
        v = body.get(key)
        return default if v is None or v == "" else int(v)

    try:
        confirm_mb = _int_field("confirm_mb", 1)
        critic_limit = _int_field("critic_limit", 500)
        if confirm_mb < 0 or critic_limit < 0:
            raise ValueError("negative")
    except (TypeError, ValueError):
        return {"ok": False, "error": "confirm_mb and critic_limit must be "
                                      "non-negative integers"}
    chain_arg = body.get("chain") or ""
    chain = tuple(c.strip() for c in str(chain_arg).split(",") if c.strip()) or None
    cross_check = bool(body.get("cross_check"))
    live_search = bool(body.get("live_search"))
    # Hints reuse (resumability, same as the CLI's --hints): a prior run's
    # persisted critic answers pre-seed this analyze so the residue loop
    # doesn't re-pay the whole critic panel every iteration.
    hints_path = str(body.get("hints_path") or "").strip() or None
    argv = (["pilot"] + (sources or []) + [f"--under={u}" for u in under]
            + [f"--confirm-mb={confirm_mb}", f"--critic-limit={critic_limit}"]
            + ([f"--chain={','.join(chain)}"] if chain else [])
            + (["--cross-check"] if cross_check else [])
            + (["--live-search"] if live_search else [])
            + ([f"--hints={hints_path}"] if hints_path else []))

    def work(cfg: Config, store: Store, progress):
        def go(run_id):
            res = pilotmod.analyze(
                store, cfg, run_id, sources=sources, under=under,
                confirm_bytes=confirm_mb * 1024 * 1024, chain=chain,
                critic_limit=critic_limit, cross_check=cross_check,
                hints_path=hints_path, live_search=live_search,
                drive_of=_PILOT_DRIVE_OF, progress=progress)
            return {
                "kind": "analyze",
                "proposal_run": run_id,
                "proposal_path": _disp(res.proposal_path),
                "summary_path": _disp(res.summary_path),
                "exit_code": res.exit_code,
                "sections": [{"id": s.id, "kind": s.kind, "status": s.status,
                              "n_rows": s.n_rows} for s in res.sections],
                "review": {"hinted": res.review.get("hinted", 0),
                           "unsure": len(res.review.get("unsure_relpaths", []))},
            }
        return _run(cfg, store, "pilot", argv, go)

    return _job_start("analyze", cfg_path, work)


# ── the proposal, served for review ───────────────────────────────────────────

def _proposal_file(workspace: str, run: str | None = None) -> str | None:
    """Newest .mlo/runs/*/proposal.json, or a specific run's. The run id is
    sanitized exactly like report's artifact names, so a query string can
    never traverse outside the runs directory."""
    runs = os.path.join(workspace, "runs")
    if run:
        p = os.path.join(runs, report._safe_name(run), "proposal.json")
        return p if os.path.exists(p) else None
    if not os.path.isdir(runs):
        return None
    for rid in sorted(os.listdir(runs), reverse=True):
        p = os.path.join(runs, rid, "proposal.json")
        if os.path.exists(p):
            return p
    return None


_NO_PROPOSAL = "no proposal found - run Analyze (Pass 1) first"


def _proposal_view(doc: dict) -> dict:
    """Display copy of a sealed proposal: PATHS go through _disp (lossy, for
    humans); every actionable identifier — section/cluster/op ids and the
    proposal seal — passes through untouched so the approvals the browser
    posts back bind losslessly (C25 depends on exact ids)."""
    out = dict(doc)
    if "library_root" in out:
        out["library_root"] = _disp(out["library_root"])
    secs = []
    for s in doc.get("sections", []):
        s = dict(s)
        if s.get("plan_path"):
            s["plan_path"] = _disp(s["plan_path"])
        s["notes"] = [_disp(n) for n in s.get("notes", [])]
        s["builder_args"] = _disp_deep(s.get("builder_args", {}))
        clusters = []
        for c in s.get("clusters", []):
            c = dict(c)
            c["sample"] = [{"op_id": m["op_id"], "src": _disp(m["src"]),
                            "dst": _disp(m["dst"])} for m in c.get("sample", [])]
            clusters.append(c)
        s["clusters"] = clusters
        secs.append(s)
    out["sections"] = secs
    review = dict(doc.get("review") or {})
    for k in ("review_set_path", "answers_path", "dissent_path", "hints_path"):
        if review.get(k):
            review[k] = _disp(review[k])
    review["unsure_relpaths"] = [_disp(r)
                                 for r in review.get("unsure_relpaths", [])]
    out["review"] = review
    out["staging_preview"] = {_disp(k): v for k, v in
                              (doc.get("staging_preview") or {}).items()}
    return out


def act_proposal(cfg_path: str, run: str | None = None) -> dict:
    """The sealed proposal (seal verified server-side by report.read_proposal;
    a tampered file surfaces as a PlanIntegrityError banner), plus the critic
    answers sidecar when the review block names one. Resolved via the
    sanitized run id ONLY — a raw path parameter would let any sealed
    proposal anywhere on disk be served (super-review B-030)."""
    ppath = _proposal_file(_workspace(cfg_path), run)
    if not ppath:
        return {"ok": False, "error": _NO_PROPOSAL}
    doc = report.read_proposal(ppath)
    answers: dict = {}
    apath = (doc.get("review") or {}).get("answers_path")
    if apath and os.path.exists(winpath.to_long(apath)):
        with open(winpath.to_long(apath), encoding="utf-8") as f:
            answers = json.load(f)
    return {"ok": True, "path": _disp(ppath),
            "proposal": _proposal_view(doc),
            "critic_answers": _disp_deep(answers)}


def _review_join(doc: dict) -> tuple[dict, dict, str | None]:
    """relpath -> review-set item and relpath -> critic answer, for joining
    onto plan rows. A missing sidecar degrades to no signals WITH a note —
    degraded output is fine, silent degradation is not."""
    review = doc.get("review") or {}
    items: dict[str, dict] = {}
    note = None
    rsp = review.get("review_set_path")
    if rsp:
        try:
            with open(winpath.to_long(rsp), encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i == 0:          # header line: {schema, count}
                        continue
                    it = json.loads(line)
                    items[it["relpath"]] = it
        except (OSError, ValueError) as e:
            note = f"review-set unavailable ({e}) - rows shown without signals"
    answers: dict[str, dict] = {}
    ap = review.get("answers_path")
    if ap and os.path.exists(winpath.to_long(ap)):
        try:
            with open(winpath.to_long(ap), encoding="utf-8") as f:
                answers = json.load(f)
        except (OSError, ValueError) as e:
            note = (note + "; " if note else "") + \
                f"critic answers unavailable ({e})"
    return items, answers, note


def act_proposal_rows(cfg_path: str, run: str | None, section: str | None,
                      cluster: str | None, offset, limit) -> dict:
    """A page of plan rows for one section (optionally one cluster), each
    joined with its review-set signals and critic answer by library relpath.
    The plan file stays the row-level truth (read_plan re-verifies its hash);
    the cluster filter reuses the executor's own _cluster_id_for_row so the
    rows shown are exactly the rows that id would execute."""
    if not section:
        return {"ok": False, "error": "section id required"}
    try:
        offset = max(0, int(offset if offset is not None else 0))
        limit = max(1, min(int(limit if limit is not None else 100), 500))
    except (TypeError, ValueError):
        return {"ok": False, "error": "offset and limit must be integers"}
    ppath = _proposal_file(_workspace(cfg_path), run)
    if not ppath:
        return {"ok": False, "error": _NO_PROPOSAL}
    doc = report.read_proposal(ppath)
    sec = next((s for s in doc.get("sections", []) if s["id"] == section), None)
    if sec is None:
        return {"ok": False, "error": f"unknown section '{section}' in this proposal"}
    if not sec.get("plan_path"):
        return {"ok": True, "section": section, "cluster": cluster, "rows": [],
                "total": 0, "offset": 0, "limit": limit,
                "note": f"section is {sec['status']} - its plan is built in "
                        f"Pass 2, so there are no rows to show yet"}
    cfg = load(cfg_path)
    rows = _plan_rows_cached(sec["plan_path"])
    if cluster:
        rows = [r for r in rows
                if pilotmod._cluster_id_for_row(sec["kind"], r, cfg) == cluster]
    total = len(rows)
    page = rows[offset:offset + limit]
    signals, answers, note = _review_join(doc)
    lib = cfg.library_root
    out_rows = []
    for r in page:
        rel = os.path.relpath(r["src"], lib) if r["src"].startswith(lib) else None
        item = signals.get(rel) if rel else None
        ans = answers.get(rel) if rel else None
        out_rows.append({
            "op_id": r["op_id"], "kind": r["kind"],
            "src": _disp(r["src"]), "dst": _disp(r["dst"]),
            "size": (r.get("pre") or {}).get("size"),
            "rule": (r.get("reason") or {}).get("rule", ""),
            "signals": _disp_deep(item) if item else None,
            "critic": _disp_deep(ans) if ans else None,
        })
    res = {"ok": True, "section": section, "cluster": cluster, "rows": out_rows,
           "total": total, "offset": offset, "limit": limit}
    if note:
        res["signals_note"] = note
    return res


# ── Pass 2: execute the approved sections ─────────────────────────────────────

# Sealed plan files are immutable once written, so a (path, mtime)-keyed
# cache is honest — and paging a 30K-row plan no longer re-parses and
# re-hashes the whole JSONL on every page request.
_PLAN_ROWS_CACHE: dict[str, tuple[float, list]] = {}


def _plan_rows_cached(path: str) -> list:
    try:
        mt = os.path.getmtime(path)
    except OSError:
        mt = -1.0
    hit = _PLAN_ROWS_CACHE.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    _, rows, _ = report.read_plan(path)
    if len(_PLAN_ROWS_CACHE) >= 8:                 # a handful of open plans
        try:            # ThreadingHTTPServer: concurrent eviction must not 500
            _PLAN_ROWS_CACHE.pop(next(iter(_PLAN_ROWS_CACHE)), None)
        except (StopIteration, RuntimeError):
            pass
    _PLAN_ROWS_CACHE[path] = (mt, rows)
    return rows


def act_pilot_execute(cfg_path: str, body: dict) -> dict:
    """Persist the approvals (audit) FIRST, then execute. The approvals dict
    must carry proposal_sha256 — pilot.execute refuses a stale binding
    (ApprovalsError -> banner), which is the whole C25 point: what the human
    approved is provably what executes."""
    approvals = body.get("approvals")
    if not isinstance(approvals, dict) or \
            not isinstance(approvals.get("decisions"), dict):
        return {"ok": False, "error": "approvals need a 'decisions' object"}
    if not approvals.get("proposal_sha256"):
        return {"ok": False, "error": "approvals must carry proposal_sha256 - "
                                      "reload the proposal and re-review"}
    approvals.setdefault("schema", pilotmod.APPROVALS_SCHEMA)
    if approvals["schema"] != pilotmod.APPROVALS_SCHEMA:
        return {"ok": False,
                "error": f"unknown approvals schema '{approvals['schema']}' "
                         f"(expected {pilotmod.APPROVALS_SCHEMA})"}
    ppath = _proposal_file(_workspace(cfg_path), body.get("run"))
    if not ppath:
        return {"ok": False, "error": _NO_PROPOSAL}

    def work(cfg: Config, store: Store, progress):
        def go(run_id):
            # Audit trail before anything moves: the exact decisions land in
            # the run directory first (same order the CLI --approve-all uses).
            report.write_json(store.workspace, run_id, "approvals", approvals)
            res = pilotmod.execute(store, cfg, run_id, ppath, approvals,
                                   drive_of=_PILOT_DRIVE_OF, progress=progress)
            return {
                "kind": "execute",
                "outcomes": [{
                    "id": o.id, "status": o.status, "cycles": o.cycles,
                    "unconverged_rows": o.unconverged_rows, "drift": o.drift,
                    "rejected_dropped": o.rejected_dropped,
                    "detail": _disp(o.detail),
                    "done": sum(c.get("done", 0) for c in o.counts_by_cycle),
                } for o in res.outcomes],
                "verify": res.verify,
                "staging": res.staging,
                "summary_path": _disp(res.summary_path),
                "exit_code": res.exit_code,
            }
        return _run(cfg, store, "pilot --execute", ["pilot", "--execute"], go)

    return _job_start("execute", cfg_path, work)


def act_latest_summary(cfg_path: str) -> dict:
    """Newest run summary.json — the same artifact the agent interface reads
    (formats.md), rendered for the final-report panel."""
    runs = os.path.join(_workspace(cfg_path), "runs")
    if os.path.isdir(runs):
        for rid in sorted(os.listdir(runs), reverse=True):
            p = os.path.join(runs, rid, "summary.json")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    return {"ok": True, "run": rid, "path": _disp(p),
                            "summary": json.load(f)}
    return {"ok": False, "error": "no run summary yet"}


# ── HTTP plumbing ─────────────────────────────────────────────────────────────

def _q1(qs: dict, key: str) -> str | None:
    v = qs.get(key)
    return v[0] if v else None


_ROUTES = {
    "/api/setup": lambda cp, b: act_setup(cp, b.get("library_root", ""),
                                          b.get("source_name", ""),
                                          b.get("source_root", "")),
    "/api/scan": lambda cp, b: act_scan(cp, b.get("target", "library")),
    "/api/verdicts": lambda cp, b: act_verdicts(cp),
    "/api/plan": lambda cp, b: act_plan(cp),
    "/api/apply": lambda cp, b: act_apply(cp, b.get("plan_path", ""),
                                          bool(b.get("execute"))),
    "/api/verify": lambda cp, b: act_verify(cp),
    "/api/pilot/analyze": lambda cp, b: act_pilot_analyze(cp, b),
    "/api/pilot/execute": lambda cp, b: act_pilot_execute(cp, b),
}

_GET_ROUTES = {
    "/api/state": lambda cp, q: act_state(cp),
    "/api/pilot/status": lambda cp, q: act_pilot_status(),
    "/api/proposal": lambda cp, q: act_proposal(cp, run=_q1(q, "run")),
    "/api/proposal/rows": lambda cp, q: act_proposal_rows(
        cp, run=_q1(q, "run"), section=_q1(q, "section"),
        cluster=_q1(q, "cluster"), offset=_q1(q, "offset"),
        limit=_q1(q, "limit")),
    "/api/runs/latest/summary": lambda cp, q: act_latest_summary(cp),
}


# POST routes that mutate through the kernel on the request thread (pilot
# analyze/execute go through the _JOB gate internally). One at a time, and
# never concurrently with a running job — the engine is a single-mutator
# design (architecture §13); a second writer can race the journal.
_MUTATING = {"/api/setup", "/api/scan", "/api/verdicts", "/api/plan",
             "/api/apply", "/api/verify"}
_ACTION_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    config_path = "mlo.toml"
    # Per-session CSRF token. serve() always sets one; when set, every POST
    # must echo it in X-MLO-Token and carry a loopback Host header — a
    # cross-origin page or DNS-rebound request can POST to 127.0.0.1 but can
    # neither read the token out of this page nor forge the Host check.
    session_token: str | None = None

    def log_message(self, *a):     # keep the console clean
        pass

    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def do_GET(self) -> None:
        if self.session_token and not self._host_ok():
            self._json({"error": "forbidden (bad Host)"}, 403)
            return
        u = urlsplit(self.path)
        if u.path in ("/", "/index.html"):
            body = INDEX_HTML.replace(
                "__MLO_TOKEN__", self.session_token or "").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        route = _GET_ROUTES.get(u.path)
        if route is None:
            self._json({"error": "not found"}, 404)
            return
        q = parse_qs(u.query)
        self._safe(lambda: route(self.config_path, q))

    def do_POST(self) -> None:
        if self.session_token:
            if not self._host_ok():
                self._json({"error": "forbidden (bad Host)"}, 403)
                return
            if not hmac.compare_digest(
                    self.headers.get("X-MLO-Token") or "",
                    self.session_token):
                self._json({"error": "forbidden (missing or wrong "
                                     "X-MLO-Token)"}, 403)
                return
        route = _ROUTES.get(self.path)
        if route is None:
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"ok": False, "error": "bad request body"}, 400)
            return
        if self.path in _MUTATING:
            if _JOB is not None and not _JOB["finished"]:
                self._json({"ok": False,
                            "error": "a job is running — wait for it"}, 409)
                return
            if not _ACTION_LOCK.acquire(blocking=False):
                self._json({"ok": False,
                            "error": "another operation is running"}, 409)
                return
            try:
                self._safe(lambda: route(self.config_path, body))
            finally:
                _ACTION_LOCK.release()
            return
        self._safe(lambda: route(self.config_path, body))

    def _safe(self, thunk) -> None:
        """Run an action; map engine refusals to a rendered remedy, not a 500."""
        try:
            self._json(thunk())
        except _EXPECTED as e:
            self._json({"ok": False, "error": str(e)})
        except Exception as e:      # unexpected — surface it, don't hide it
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)


def serve(config_path: str = "mlo.toml", port: int = 8765) -> int:
    import secrets
    Handler.config_path = os.path.abspath(config_path)
    Handler.session_token = secrets.token_urlsafe(16)
    # The same workspace .env the CLI loads in _open() (P21/C61) — without
    # this, critic chains that work from the CLI silently lose their
    # MLO_*_KEY credentials under the web UI (super-review H4). Process env
    # always wins; jobs run on threads of this process, so one boot-time
    # load covers every later _cfg_store().
    from .dotenv import load_dotenv
    load_dotenv(os.path.join(_workspace(Handler.config_path), ".env"))
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"cannot bind 127.0.0.1:{port}: {e} — is another mlo serve "
              f"running? Pass --port to pick a different one.",
              file=sys.stderr)
        return 2
    _boot_pending_warning(Handler.config_path)

    url = f"http://127.0.0.1:{port}/"
    print(f"mlo web UI on {url}   (config: {Handler.config_path})")
    print("Analyze (Pass 1) -> review the proposal -> Execute (Pass 2)."
          " Guided mode is on the last tab. Ctrl-C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        # A running job mutates through the kernel on a worker thread; a
        # daemon-kill mid-op is recoverable (the journal + reconcile exist
        # for exactly that) but finishing cleanly is better than recovering.
        if _JOB is not None and not _JOB["finished"]:
            print(f"\na {_JOB['kind']} job is still running — waiting for it "
                  f"to finish (its journal keeps every step crash-safe) …")
            while _JOB is not None and not _JOB["finished"]:
                time.sleep(0.5)
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="mlo-token" content="__MLO_TOKEN__">
<title>mlo — control room</title>
<style>
  /* ── tokens: neutral surface scale, ONE accent, status hues ─────────── */
  :root{
    --bg:#12151b; --panel:#191d25; --panel2:#10131a; --inset:#0d1016;
    --line:#272d39; --line2:#323a49;
    --ink:#e9edf3; --dim:#9aa5b4; --faint:#6d7789;
    --accent:#4c8dff; --accent-soft:rgba(76,141,255,.13);
    --ok:#43bd82; --ok-soft:rgba(67,189,130,.13);
    --warn:#e0a63d; --warn-soft:rgba(224,166,61,.13);
    --bad:#e26a62; --bad-soft:rgba(226,106,98,.12);
    --mono:ui-monospace,"Cascadia Mono",Consolas,monospace;
    --shadow:0 1px 2px rgba(0,0,0,.25);
    color-scheme:dark;
  }
  @media (prefers-color-scheme: light){
    :root{
      --bg:#f4f5f8; --panel:#ffffff; --panel2:#eef0f4; --inset:#f7f8fa;
      --line:#dfe3ea; --line2:#cbd2dd;
      --ink:#1b2331; --dim:#5c6678; --faint:#8d95a6;
      --accent:#2e6ce0; --accent-soft:rgba(46,108,224,.10);
      --ok:#1e8a52; --ok-soft:rgba(30,138,82,.11);
      --warn:#996a10; --warn-soft:rgba(153,106,16,.12);
      --bad:#c23b32; --bad-soft:rgba(194,59,50,.09);
      --shadow:0 1px 2px rgba(15,23,42,.06);
      color-scheme:light;
    }
  }
  *{box-sizing:border-box}
  html{scrollbar-gutter:stable}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.55 "Segoe UI",system-ui,-apple-system,sans-serif;
       -webkit-font-smoothing:antialiased}
  .num,td.num,.kv b{font-variant-numeric:tabular-nums}
  code,.mono{font-family:var(--mono);font-size:12.5px}
  a{color:var(--accent)}

  /* ── header + tabs ───────────────────────────────────────────────────── */
  header{display:flex;align-items:center;gap:26px;flex-wrap:wrap;
         padding:14px 26px 0;border-bottom:1px solid var(--line);
         background:var(--panel);position:sticky;top:0;z-index:30}
  .brand{display:flex;align-items:baseline;gap:10px;padding-bottom:12px}
  .brand h1{margin:0;font-size:17px;letter-spacing:.01em}
  .brand .sub{color:var(--faint);font-size:12px}
  nav{display:flex;gap:2px;margin-left:auto}
  nav button{appearance:none;border:0;background:none;color:var(--dim);
    font:inherit;font-size:13px;font-weight:600;padding:9px 14px 13px;
    cursor:pointer;border-bottom:2px solid transparent}
  nav button:hover{color:var(--ink)}
  nav button.on{color:var(--ink);border-bottom-color:var(--accent)}
  main{max-width:1060px;margin:0 auto;padding:22px 20px 90px}
  .tab{display:none}.tab.on{display:block}

  /* ── cards, grids, chips ─────────────────────────────────────────────── */
  .card{background:var(--panel);border:1px solid var(--line);
        border-radius:10px;padding:16px 18px;margin:0 0 14px;
        box-shadow:var(--shadow)}
  .card h2{margin:0 0 4px;font-size:14.5px}
  .card h3{margin:0 0 6px;font-size:13px}
  .hint{color:var(--dim);font-size:12.5px;margin:2px 0 10px}
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));
         gap:12px;margin-bottom:14px}
  .kv{display:flex;flex-direction:column;gap:2px}
  .kv b{font-size:21px;font-weight:650}
  .kv span{color:var(--dim);font-size:12px}
  .chip{display:inline-flex;align-items:center;gap:5px;padding:1px 9px 2px;
        border-radius:999px;font-size:11px;font-weight:650;letter-spacing:.03em;
        white-space:nowrap;border:1px solid transparent}
  .chip.ok{background:var(--ok-soft);color:var(--ok)}
  .chip.warn{background:var(--warn-soft);color:var(--warn)}
  .chip.bad{background:var(--bad-soft);color:var(--bad)}
  .chip.dim{background:var(--panel2);color:var(--faint)}
  .chip.acc{background:var(--accent-soft);color:var(--accent)}
  .chip.out{background:none;border-color:var(--line2);color:var(--dim)}

  /* ── forms + buttons ─────────────────────────────────────────────────── */
  label{display:block;font-size:12px;color:var(--dim);margin:10px 0 4px}
  input[type=text],input[type=number],textarea{width:100%;padding:8px 10px;
    background:var(--inset);border:1px solid var(--line);border-radius:7px;
    color:var(--ink);font:12.5px var(--mono)}
  input:focus,textarea:focus{outline:2px solid var(--accent-soft);
    border-color:var(--accent)}
  .check{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--ink)}
  .check input{width:auto}
  button.btn{border:1px solid var(--line2);border-radius:7px;padding:8px 15px;
    font:inherit;font-size:13px;font-weight:600;cursor:pointer;
    background:var(--panel2);color:var(--ink)}
  button.btn:hover{border-color:var(--faint)}
  button.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  button.btn.primary:hover{filter:brightness(1.08)}
  button.btn.danger{background:var(--bad);border-color:var(--bad);color:#fff}
  button.btn.ghost{background:none}
  button.btn.sm{padding:4px 10px;font-size:12px}
  button:disabled{opacity:.45;cursor:default}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:12px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 22px}
  @media(max-width:760px){.grid2{grid-template-columns:1fr}}

  /* ── banners ─────────────────────────────────────────────────────────── */
  .banner{border-radius:8px;padding:10px 13px;font-size:13px;margin:10px 0;
          white-space:pre-wrap;overflow-wrap:anywhere}
  .banner.err{background:var(--bad-soft);color:var(--bad);
              border:1px solid color-mix(in srgb,var(--bad) 35%,transparent)}
  .banner.warn{background:var(--warn-soft);color:var(--warn);
              border:1px solid color-mix(in srgb,var(--warn) 35%,transparent)}
  .banner.okay{background:var(--ok-soft);color:var(--ok);
              border:1px solid color-mix(in srgb,var(--ok) 35%,transparent)}

  /* ── phase checklist ─────────────────────────────────────────────────── */
  ol.phases{list-style:none;margin:8px 0 0;padding:0}
  ol.phases li{display:flex;gap:10px;align-items:baseline;padding:5px 0;
               border-bottom:1px dashed var(--line);font-size:13px}
  ol.phases li:last-child{border-bottom:0}
  .dot{flex:0 0 auto;width:9px;height:9px;border-radius:50%;
       background:var(--line2);position:relative;top:-1px}
  li.done .dot{background:var(--ok)}
  li.active .dot{background:var(--accent);box-shadow:0 0 0 4px var(--accent-soft)}
  li.err .dot{background:var(--bad)}
  li .pname{min-width:170px;font-weight:600}
  li.pending .pname{color:var(--faint);font-weight:400}
  li .pinfo{color:var(--dim);font-size:12px;font-family:var(--mono);
            overflow-wrap:anywhere}

  /* ── section cards (review) ──────────────────────────────────────────── */
  .sec{border:1px solid var(--line);border-radius:10px;background:var(--panel);
       margin:0 0 12px;box-shadow:var(--shadow)}
  .sec>.head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
             padding:12px 16px}
  .sec .sid{font-family:var(--mono);font-size:13px;font-weight:650}
  .sec .meta{color:var(--dim);font-size:12px;display:flex;gap:14px;
             margin-left:auto;align-items:center}
  .sec .meta b{color:var(--ink);font-weight:650}
  .sec>.body{padding:0 16px 13px}
  details.notes{margin:6px 0 0}
  details summary{cursor:pointer;color:var(--dim);font-size:12.5px;
                  user-select:none}
  details summary:hover{color:var(--ink)}
  details .note{color:var(--dim);font-size:12.5px;margin:4px 0 0 14px}
  .seg{display:inline-flex;border:1px solid var(--line2);border-radius:7px;
       overflow:hidden}
  .seg button{appearance:none;border:0;background:none;color:var(--dim);
    font:inherit;font-size:12px;font-weight:650;padding:4px 12px;cursor:pointer}
  .seg button+button{border-left:1px solid var(--line2)}
  .seg button.on.app{background:var(--ok-soft);color:var(--ok)}
  .seg button.on.rej{background:var(--bad-soft);color:var(--bad)}
  .mixtag{font-size:11px;color:var(--warn);font-weight:650}

  table.tbl{width:100%;border-collapse:collapse;margin:8px 0 2px;font-size:12.5px}
  table.tbl th{text-align:left;color:var(--faint);font-size:10.5px;
    text-transform:uppercase;letter-spacing:.06em;font-weight:650;
    padding:6px 8px;border-bottom:1px solid var(--line)}
  table.tbl td{padding:6px 8px;border-bottom:1px solid var(--line);
    vertical-align:top}
  table.tbl tr:last-child td{border-bottom:0}
  table.tbl td.num,table.tbl th.num{text-align:right}
  .clab{font-family:var(--mono);font-size:12px;overflow-wrap:anywhere}
  .crule{color:var(--faint);font-size:11px;font-family:var(--mono)}
  .samp{font-family:var(--mono);font-size:11.5px;color:var(--dim);
        overflow-wrap:anywhere;margin:2px 0}
  .samp .arr{color:var(--accent)}
  .scroll{overflow-x:auto}

  /* ── rows drill-down / modal ─────────────────────────────────────────── */
  #overlay{position:fixed;inset:0;background:rgba(8,10,14,.55);z-index:80;
           display:flex;align-items:flex-start;justify-content:center;
           padding:5vh 16px}
  #overlay[hidden]{display:none}
  #modal{background:var(--panel);border:1px solid var(--line2);
         border-radius:12px;max-width:940px;width:100%;max-height:88vh;
         overflow:auto;padding:18px 20px;box-shadow:0 12px 40px rgba(0,0,0,.4)}
  #modal h2{margin:0 0 2px;font-size:14.5px}
  .rowitem{border:1px solid var(--line);border-radius:8px;padding:10px 12px;
           margin:8px 0;background:var(--panel2)}
  .rowpath{font-family:var(--mono);font-size:12px;overflow-wrap:anywhere}
  .rowpath .arr{color:var(--accent);padding:0 2px}
  .rowmeta{display:flex;gap:12px;flex-wrap:wrap;color:var(--dim);
           font-size:11.5px;margin-top:5px;align-items:center}
  .sig{margin-top:7px;border-top:1px dashed var(--line);padding-top:7px;
       font-size:12px;color:var(--dim)}
  .sig b{color:var(--ink);font-weight:600}
  .sig .tag{display:inline-block;background:var(--inset);
    border:1px solid var(--line);border-radius:5px;padding:0 6px;margin:1px 3px 1px 0;
    font-family:var(--mono);font-size:11px;overflow-wrap:anywhere}
  .critic{margin-top:7px;background:var(--accent-soft);border-radius:7px;
          padding:8px 10px;font-size:12.5px}
  .critic .conf{font-weight:650;font-variant-numeric:tabular-nums}
  .pager{display:flex;gap:10px;align-items:center;margin-top:10px;
         color:var(--dim);font-size:12.5px}

  /* ── execute bar + report ────────────────────────────────────────────── */
  .execbar{position:sticky;bottom:0;z-index:20;display:flex;gap:16px;
    align-items:center;background:var(--panel);border:1px solid var(--line2);
    border-radius:10px;padding:12px 16px;margin-top:16px;
    box-shadow:0 -4px 18px rgba(0,0,0,.18)}
  .execbar .tot{font-size:13px}
  .execbar .tot b{font-variant-numeric:tabular-nums}
  .never{color:var(--faint);font-size:11.5px}
  .queue-list{max-height:260px;overflow:auto;background:var(--inset);
    border:1px solid var(--line);border-radius:7px;padding:8px 10px;
    font-family:var(--mono);font-size:11.5px;color:var(--dim);
    overflow-wrap:anywhere}
  .queue-list div{padding:1px 0}
  .verdictline{display:flex;gap:10px;align-items:center;padding:6px 0;
    border-bottom:1px dashed var(--line);font-size:13px;flex-wrap:wrap}
  .verdictline:last-child{border-bottom:0}
  .verdictline .sid{font-family:var(--mono);font-weight:650;font-size:12.5px}
  .verdictline .d{color:var(--dim);font-size:12px;margin-left:auto;
                  font-variant-numeric:tabular-nums}

  /* ── guided stepper (kept from v1, re-skinned onto the tokens) ───────── */
  .step{background:var(--panel);border:1px solid var(--line);border-radius:10px;
        margin:14px 0;padding:16px 18px;opacity:.45;transition:opacity .2s}
  .step.active,.step.done{opacity:1}
  .step h2{margin:0 0 2px;font-size:14px;display:flex;align-items:center;gap:8px}
  .stepnum{display:inline-flex;width:21px;height:21px;border-radius:50%;
    background:var(--panel2);color:var(--dim);font-size:11.5px;
    align-items:center;justify-content:center;flex:0 0 auto}
  .step.active .stepnum{background:var(--accent);color:#fff}
  .step.done .stepnum{background:var(--ok);color:#fff}
  .out{margin-top:12px}
  .err{color:var(--bad);white-space:pre-wrap;font-size:13px}
  .ok{color:var(--ok)}
  .pills{display:flex;gap:10px;flex-wrap:wrap}
  .pill{background:var(--inset);border:1px solid var(--line);border-radius:8px;
        padding:8px 12px;min-width:96px}
  .pill b{display:block;font-size:19px;font-variant-numeric:tabular-nums}
  .pill span{color:var(--dim);font-size:11.5px}
  .tree{font:12px var(--mono);background:var(--inset);
        border:1px solid var(--line);border-radius:8px;padding:12px 14px;
        max-height:360px;overflow:auto}
  .tnode{white-space:pre}
  .tcount{color:var(--dim)}
  .samples{margin-top:10px;font:11.5px var(--mono);color:var(--dim);
           max-height:150px;overflow:auto}
  .note{color:var(--dim);font-size:12.5px;margin:3px 0}
</style>
</head>
<body>
<header>
  <div class="brand"><h1>mlo</h1><span class="sub">organize + dedup · staging-only, never deletes</span></div>
  <nav id="tabs">
    <button data-tab="home" class="on">Overview</button>
    <button data-tab="analyze">1 · Analyze</button>
    <button data-tab="review">2 · Review</button>
    <button data-tab="run">3 · Execute</button>
    <button data-tab="guided">Guided mode</button>
  </nav>
</header>
<main>

<section id="tab-home" class="tab on">
  <div id="homeIntro" class="card">
    <h2>Two passes. One review. Nothing moves until you approve it.</h2>
    <p class="hint"><b>Pass 1 — Analyze</b> runs everything read-only and rehearsed, and produces one sealed proposal.
       <b>Pass 2 — Execute</b> runs exactly the sections you approve, hash-bound to that proposal, then verifies.
       Duplicates are <i>staged</i> to a same-drive folder — mlo never deletes; disposal of staged files stays yours.</p>
    <div class="row">
      <button class="btn primary" id="homeAnalyze">Analyze everything (Pass 1)</button>
      <button class="btn" id="homeReview">Open the latest proposal</button>
      <button class="btn ghost" id="homeGuided">Guided mode (one folder at a time)</button>
    </div>
  </div>
  <div id="homeCards" class="cards"></div>
  <div id="homeExtra"></div>
</section>

<section id="tab-analyze" class="tab">
  <div class="card">
    <h2>Pass 1 — analyze everything</h2>
    <p class="hint">Read-only + rehearsed: library scan, per-source verdicts, every applicable plan,
      the full-signal review set, the critic panel (if [llm] is enabled), a hinted re-plan, and a
      per-section rehearsal. Output: one sealed proposal to review. The ops journal is untouched.</p>
    <div class="grid2">
      <div>
        <label>Sources to analyze</label>
        <div id="anSources" class="hint">loading…</div>
        <label>Library scope prefixes — optional, one per line (empty = whole library)</label>
        <textarea id="anUnder" rows="3" spellcheck="false" placeholder="Video&#10;Audio\Music"></textarea>
      </div>
      <div>
        <div class="grid2">
          <div><label>Dedup re-confirm (MiB)</label><input type="number" id="anConfirm" value="1" min="0"></div>
          <div><label>Critic limit</label><input type="number" id="anLimit" value="500" min="0"></div>
        </div>
        <label>Critic chain override — optional, comma-separated</label>
        <input type="text" id="anChain" spellcheck="false" placeholder="claude-opus-4-8,local">
        <label>Reuse prior critic answers — optional hints JSON path</label>
        <input type="text" id="anHints" spellcheck="false"
               placeholder="(pilot-hints path from a prior proposal)">
        <label class="check" style="margin-top:14px"><input type="checkbox" id="anCross">
          Cross-check: second critic + adversarial tiebreak (token-costly)</label>
        <div id="anLlmNote" class="hint"></div>
      </div>
    </div>
    <div class="row"><button class="btn primary" id="anGo">Analyze — build the proposal</button></div>
    <div id="anNote"></div>
  </div>
  <div class="card" id="anProgressCard" hidden>
    <h2>Analysis progress</h2>
    <ol id="anPhases" class="phases"></ol>
    <div id="anOut"></div>
  </div>
</section>

<section id="tab-review" class="tab">
  <div id="revHead"></div>
  <div id="revBody"><div class="card"><p class="hint">loading…</p></div></div>
  <div id="revFoot" class="execbar" hidden>
    <div class="tot" id="revTotals"></div>
    <span class="never">mlo never deletes - disposal of staged files stays yours</span>
    <button class="btn danger" id="revExec" style="margin-left:auto">Execute approved sections…</button>
  </div>
</section>

<section id="tab-run" class="tab">
  <div class="card" id="runProgressCard">
    <h2>Pass 2 — execution</h2>
    <div id="runPhases" class="hint">No execution running. Approve sections on the Review tab, then execute from there.</div>
  </div>
  <div id="runOut"></div>
</section>

<section id="tab-guided" class="tab">
  <div class="card">
    <h2>Guided mode</h2>
    <p class="hint">The original one-source stepper: point at a messy folder and your library, review each
      interim output, then execute the organize step. For multi-source analysis, dedup and library repair,
      use the 2-pass flow instead.</p>
  </div>
  <div id="guided"></div>
</section>

</main>
<div id="overlay" hidden><div id="modal"></div></div>
<script>
"use strict";
const $  = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));
const esc = s => String(s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

const MLO_TOKEN = document.querySelector('meta[name="mlo-token"]')?.content || "";
async function api(path, body) {
  const opt = body === undefined ? {} :
    { method:"POST",
      headers:{"Content-Type":"application/json", "X-MLO-Token":MLO_TOKEN},
      body:JSON.stringify(body) };
  const r = await fetch(path, opt);
  return r.json();
}
function fmtInt(n){ return n==null ? "–" : Number(n).toLocaleString("en-US"); }
function fmtBytes(n){
  if(n==null) return "–";
  const u=["B","KB","MB","GB","TB"]; let x=Number(n), i=0;
  while(x>=1024 && i<u.length-1){ x/=1024; i++; }
  return (i ? x.toFixed(x>=10?0:1) : String(Math.round(x))) + " " + u[i];
}
function chip(text, cls){ return `<span class="chip ${cls}">${esc(text)}</span>`; }
const STATUS_CLS = {ready:"ok", gated:"warn", blocked:"bad", empty:"dim",
                    converged:"ok", residual:"warn", rejected:"dim",
                    "skipped-empty":"dim"};
function statusChip(s){ return chip(s, STATUS_CLS[s] || "out"); }
function banner(msg, cls){ return `<div class="banner ${cls}">${esc(msg)}</div>`; }

let STATE=null, PROPOSAL=null, ANSWERS={}, DECISIONS={}, POLLT=null;
const OPEN = new Set();   // which <details> stay open across re-renders

// ── tabs ─────────────────────────────────────────────────────────────────────
function showTab(name){
  $$("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab===name));
  $$("main > .tab").forEach(t => t.classList.toggle("on", t.id==="tab-"+name));
  if(name==="home") renderHome();
  if(name==="review" && !PROPOSAL) loadProposal();
}
$$("#tabs button").forEach(b => b.onclick = () => showTab(b.dataset.tab));
$("#homeAnalyze").onclick = () => showTab("analyze");
$("#homeReview").onclick  = () => showTab("review");
$("#homeGuided").onclick  = () => showTab("guided");

// ── overview ─────────────────────────────────────────────────────────────────
function freshChip(f){ return f ? chip("fresh","ok") : chip("stale","warn"); }
async function renderHome(){
  const st = await api("/api/state"); STATE = st;
  const cards = $("#homeCards"), extra = $("#homeExtra");
  if(!st.config_exists){
    cards.innerHTML = "";
    extra.innerHTML = `<div class="card"><h3>No config yet</h3>
      <p class="hint">There is no <code>${esc(st.config_path)}</code>. Use Guided mode to point mlo at
      your folders (it writes a starter config), or author <code>mlo.toml</code> by hand — the 2-pass flow
      needs <code>[staging]</code> roots for dedup.</p>
      <div class="row"><button class="btn primary" onclick="showTab('guided')">Open Guided mode</button></div></div>`;
    return;
  }
  if(st.config_error){
    cards.innerHTML = "";
    extra.innerHTML = banner("config error: " + st.config_error, "err");
    return;
  }
  let h = "";
  h += `<div class="card kv"><b class="num">${fmtInt(st.index_count)}</b>
        <span>library files indexed ${freshChip(!!st.index_fresh)}</span></div>`;
  h += `<div class="card kv"><b class="num">${fmtInt(st.journal_pos)}</b>
        <span>journal position — every mutation ever journaled</span></div>`;
  const srcs = st.sources || [];
  h += `<div class="card kv"><b class="num">${fmtInt(srcs.filter(s=>s.enabled).length)}</b>
        <span>enabled sources of ${fmtInt(srcs.length)} configured</span></div>`;
  const stg = Object.entries(st.staging || {});
  h += `<div class="card kv"><b class="num">${fmtInt(stg.length)}</b>
        <span>staging roots (same-drive, reversible)</span></div>`;
  cards.innerHTML = h;

  let x = "";
  x += `<div class="card"><h3>Sources</h3>`;
  x += srcs.length ? `<div class="scroll"><table class="tbl"><thead><tr>
        <th>name</th><th>root</th><th>scan</th><th>verdicts</th><th></th></tr></thead><tbody>` +
      srcs.map(s => `<tr><td class="mono">${esc(s.name)}</td>
        <td class="mono">${esc(s.root)}</td>
        <td>${s.enabled ? freshChip(s.scan_fresh) : ""}</td>
        <td>${s.enabled ? freshChip(s.verdicts_fresh) : ""}</td>
        <td>${s.enabled ? "" : chip("disabled","dim")}</td></tr>`).join("") +
      `</tbody></table></div>`
    : `<p class="hint">none configured</p>`;
  x += `</div>`;
  x += `<div class="card"><h3>Staging roots</h3>`;
  x += stg.length
    ? stg.map(([d,p]) => `<div class="samp"><b>${esc(d)}:</b> ${esc(p)}</div>`).join("")
      + `<p class="never" style="margin:8px 0 0">mlo never deletes - disposal of staged files stays yours</p>`
    : `<p class="hint">none — dedup sections need a [staging] root per drive in mlo.toml</p>`;
  x += `</div>`;
  x += `<div class="card"><h3>Latest proposal</h3>`;
  if(st.proposal){
    x += `<p class="hint">run <code>${esc(st.proposal.run)}</code> · <span class="mono">${esc(st.proposal.path)}</span></p>
          <div class="row"><button class="btn primary" onclick="showTab('review')">Open the review</button></div>`;
  } else {
    x += `<p class="hint">none yet — run Analyze (Pass 1) to build one</p>`;
  }
  x += `</div>`;
  const sum = await api("/api/runs/latest/summary");
  if(sum.ok){
    const s = sum.summary || {};
    x += `<div class="card"><h3>Latest run summary</h3>
      <p class="hint">run <code>${esc(sum.run)}</code>${s.command ? " · " + esc(s.command) : ""}
      ${s.exit_code!==undefined ? " · exit " + esc(s.exit_code) : ""}</p>
      <div class="samp">${esc(sum.path)}</div></div>`;
  }
  extra.innerHTML = x;
}

// ── analyze: launch + progress ───────────────────────────────────────────────
const AN_PHASES = [
  ["scan-library","Scan library"], ["scan-source","Scan sources"],
  ["hints","Assemble hints"], ["plan","Build plans"],
  ["review-set","Review set (full signals)"], ["critics","Critic panel"],
  ["replan","Hinted re-plan"], ["rehearse","Rehearse sections"],
  ["assemble","Seal the proposal"]];

async function fillAnalyzeForm(){
  const st = STATE || await api("/api/state");
  const box = $("#anSources");
  const srcs = (st.sources||[]).filter(s => s.enabled);
  box.innerHTML = srcs.length
    ? srcs.map(s => `<label class="check" style="margin:4px 0">
        <input type="checkbox" class="anSrc" value="${esc(s.name)}" checked>
        <span class="mono">${esc(s.name)}</span>
        <span class="hint" style="margin:0">${esc(s.root)}</span></label>`).join("")
    : `<p class="hint">no enabled sources — library-side sections only</p>`;
  $("#anLlmNote").innerHTML = st.llm_enabled
    ? `[llm] is enabled — unrouted files go to the critic panel`
    : `[llm] is disabled — the whole unrouted residue goes to the human review queue`;
}

$("#anGo").onclick = async () => {
  const note = $("#anNote");
  note.innerHTML = "";
  const all = $$(".anSrc"), picked = all.filter(c => c.checked).map(c => c.value);
  const body = {
    sources: (all.length && picked.length !== all.length) ? picked : null,
    under: $("#anUnder").value.split("\n").map(s => s.trim()).filter(Boolean),
    confirm_mb: Number($("#anConfirm").value || 1),
    critic_limit: Number($("#anLimit").value || 500),
    chain: $("#anChain").value.trim() || null,
    cross_check: $("#anCross").checked,
    hints_path: $("#anHints").value.trim() || null,
  };
  const r = await api("/api/pilot/analyze", body);
  if(!r.ok){ note.innerHTML = banner(r.error, "err"); return; }
  $("#anProgressCard").hidden = false;
  $("#anOut").innerHTML = "";
  pollJob();
};

function phaseSummary(events, phase){
  const evs = events.filter(e => e.phase === phase);
  if(!evs.length) return "";
  const last = evs[evs.length-1];
  const kv = Object.entries(last.info||{}).map(([k,v]) => k+"="+v).join("  ");
  return (evs.length>1 ? "(" + evs.length + ") " : "") + kv;
}

function renderAnalyzeProgress(j){
  const seen = new Set(j.events.map(e => e.phase));
  const lastPhase = j.events.length ? j.events[j.events.length-1].phase : null;
  const items = AN_PHASES.map(([p, label]) => {
    let cls = "pending";
    if(seen.has(p)) cls = (p === lastPhase && !j.finished) ? "active" : "done";
    else if(j.finished && !j.error) cls = "pending";   // skipped (e.g. critics off)
    if(j.error && p === lastPhase) cls = "err";
    return `<li class="${cls}"><span class="dot"></span>
      <span class="pname">${esc(label)}</span>
      <span class="pinfo">${esc(phaseSummary(j.events, p))}</span></li>`;
  });
  $("#anPhases").innerHTML = items.join("");
  if(!j.finished) return;
  const out = $("#anOut");
  if(j.error){ out.innerHTML = banner(j.error, "err"); return; }
  const res = j.result || {};
  const secs = res.sections || [];
  out.innerHTML = banner("Proposal sealed. Nothing has moved — review it, then execute the sections you approve.", "okay")
    + `<div class="scroll"><table class="tbl"><thead><tr><th>section</th><th>status</th><th class="num">rows</th></tr></thead><tbody>`
    + secs.map(s => `<tr><td class="mono">${esc(s.id)}</td><td>${statusChip(s.status)}</td>
        <td class="num">${fmtInt(s.n_rows)}</td></tr>`).join("")
    + `</tbody></table></div>`
    + `<div class="samp">${esc(res.proposal_path||"")}</div>`
    + `<div class="row"><button class="btn primary" onclick="loadProposal().then(()=>showTab('review'))">Open the review</button></div>`;
}

// ── job polling (both kinds) ─────────────────────────────────────────────────
function pollJob(){
  if(POLLT) clearInterval(POLLT);
  const tick = async () => {
    const st = await api("/api/pilot/status");
    const j = st.job;
    if(!j) return;
    if(j.kind === "analyze"){
      $("#anProgressCard").hidden = false;
      renderAnalyzeProgress(j);
    } else {
      renderExecProgress(j);
    }
    if(j.finished){
      clearInterval(POLLT); POLLT = null;
      if(!j.error && j.kind === "analyze"){ PROPOSAL = null; }
      if(j.kind === "execute"){ renderExecReport(j); }
    }
  };
  POLLT = setInterval(tick, 1000);
  tick();
}

// ── review: the proposal ─────────────────────────────────────────────────────
async function loadProposal(){
  const head = $("#revHead"), body = $("#revBody");
  const r = await api("/api/proposal");
  if(!r.ok){
    PROPOSAL = null; head.innerHTML = "";
    $("#revFoot").hidden = true;
    body.innerHTML = `<div class="card"><h3>No proposal to review</h3>
      ${banner(r.error, r.error.indexOf("no proposal")===0 ? "warn" : "err")}
      <div class="row"><button class="btn primary" onclick="showTab('analyze')">Run Analyze (Pass 1)</button></div></div>`;
    return;
  }
  PROPOSAL = r.proposal; ANSWERS = r.critic_answers || {};
  DECISIONS = {};
  for(const s of PROPOSAL.sections) DECISIONS[s.id] = {default:"reject", clusters:{}};
  renderReview();
}

function approvable(s){
  return (s.status==="ready" && s.n_rows>0) || s.status==="gated";
}
function effCluster(sid, cid){
  const d = DECISIONS[sid];
  return (d.clusters[cid]) || d.default;
}
function sectionState(s){
  // approve | reject | mixed — for the segmented control + totals
  const d = DECISIONS[s.id];
  const ov = Object.values(d.clusters);
  if(!ov.length) return d.default;
  const effs = new Set(s.clusters.map(c => effCluster(s.id, c.id)));
  return effs.size > 1 ? "mixed" : effs.values().next().value;
}
function approvedTotals(){
  let sections=0, rows=0, bytes=0, gatedRows=0;
  for(const s of PROPOSAL.sections){
    if(!approvable(s)) continue;
    const d = DECISIONS[s.id];
    let sr=0, sb=0;
    if(s.clusters && s.clusters.length){
      for(const c of s.clusters)
        if(effCluster(s.id, c.id)==="approve"){ sr += c.n_rows; sb += c.bytes; }
    } else if(d.default==="approve"){
      sr = s.n_rows; sb = s.bytes || 0;
      if(s.status==="gated") gatedRows += s.n_rows;
    }
    if(sr > 0 || (d.default==="approve" && s.status==="gated")){ sections++; rows+=sr; bytes+=sb; }
  }
  return {sections, rows, bytes, gatedRows};
}

function setSection(sid, val){
  DECISIONS[sid] = {default: val, clusters: {}};   // section toggle clears overrides
  renderReview();
}
function setCluster(sid, cid, val){
  const d = DECISIONS[sid];
  if(d.clusters[cid] === val) delete d.clusters[cid];   // click again = inherit
  else d.clusters[cid] = val;
  renderReview();
}
function bulk(val){
  for(const s of PROPOSAL.sections)
    if(approvable(s)) DECISIONS[s.id] = {default: val, clusters: {}};
  renderReview();
}

function segFor(sid, state){
  return `<span class="seg" data-sid="${esc(sid)}">
    <button class="${state==="approve" ? "on app" : ""}" data-act="sec-approve">Approve</button>
    <button class="${state==="reject" ? "on rej" : ""}" data-act="sec-reject">Reject</button>
  </span>${state==="mixed" ? ` <span class="mixtag">mixed</span>` : ""}`;
}

function clusterRow(s, c){
  const eff = effCluster(s.id, c.id);
  const ov = DECISIONS[s.id].clusters[c.id];
  const samp = c.sample.map(m =>
    `<div class="samp">${esc(m.src)} <span class="arr">&rarr;</span> ${esc(m.dst)}</div>`).join("");
  return `<tr>
    <td><span class="seg" data-sid="${esc(s.id)}" data-cid="${esc(c.id)}">
      <button class="${eff==="approve" ? "on app" : ""}${ov==="approve" ? "" : ""}" data-act="cl-approve" title="approve this cluster">&#10003;</button>
      <button class="${eff==="reject" ? "on rej" : ""}" data-act="cl-reject" title="reject this cluster">&#10005;</button>
    </span>${ov ? "" : ""}</td>
    <td><div class="clab">${esc(c.label)}</div><div class="crule">${esc(c.rule)}</div></td>
    <td class="num">${fmtInt(c.n_rows)}</td>
    <td class="num">${fmtBytes(c.bytes)}</td>
    <td><details data-open="${esc(s.id)}|s|${esc(c.id)}"><summary>sample</summary>${samp}</details></td>
    <td><button class="btn sm" data-act="rows" data-sid="${esc(s.id)}" data-cid="${esc(c.id)}">rows</button></td>
  </tr>`;
}

function sectionCard(s){
  const d = DECISIONS[s.id];
  const reh = s.rehearsal || {};
  const meta = `<span class="meta">
      <span><b class="num">${fmtInt(s.n_rows)}</b> rows</span>
      <span><b class="num">${fmtBytes(s.bytes)}</b></span>
      ${s.plan_path ? `<span title="rehearsed against live disk">rehearsal:
        <b class="num">${fmtInt(reh.would_do||0)}</b> would do
        ${reh.drift ? `· <b class="num">${fmtInt(reh.drift)}</b> drift` : ""}</span>` : ""}
    </span>`;
  let body = "";
  if(s.blocked_reason)
    body += banner(s.blocked_reason, "err");
  if(s.notes && s.notes.length)
    body += `<details class="notes" data-open="${esc(s.id)}|n">
      <summary>notes (${s.notes.length})</summary>
      ${s.notes.map(n => `<div class="note">&bull; ${esc(n)}</div>`).join("")}</details>`;
  if(s.clusters && s.clusters.length){
    body += `<details data-open="${esc(s.id)}|c"><summary>clusters (${s.clusters.length})</summary>
      <div class="scroll"><table class="tbl">
      <thead><tr><th></th><th>cluster</th><th class="num">rows</th><th class="num">bytes</th><th>sample</th><th></th></tr></thead>
      <tbody>${s.clusters.map(c => clusterRow(s, c)).join("")}</tbody></table></div></details>`;
  } else if(s.status === "gated"){
    body += `<p class="hint" style="margin:6px 0 0">Built in Pass 2 after <span class="mono">${esc((s.depends_on||[]).join(", "))}</span>
      executes — approval covers the bounded contract in the notes above. Row counts are the verdict preview.</p>`;
  }
  const state = approvable(s) ? sectionState(s) : null;
  return `<div class="sec">
    <div class="head">
      <span class="sid">${esc(s.id)}</span>
      ${statusChip(s.status)} ${chip(s.kind, "out")}
      ${meta}
      ${state ? segFor(s.id, state) : ""}
      ${s.plan_path && s.n_rows ? `<button class="btn sm" data-act="rows" data-sid="${esc(s.id)}">rows</button>` : ""}
    </div>
    ${body ? `<div class="body">${body}</div>` : ""}
  </div>`;
}

function renderReview(){
  if(!PROPOSAL) return;
  const p = PROPOSAL;
  const secs = {}; p.sections.forEach(s => secs[s.id] = s);
  const order = (p.execution_order && p.execution_order.length)
    ? p.execution_order : p.sections.map(s => s.id);
  const llm = p.llm || {};
  $("#revHead").innerHTML = `<div class="card">
    <h2>Proposal — run <code>${esc(p.run)}</code></h2>
    <p class="hint">sealed <span class="mono" title="${esc(p.proposal_sha256)}">${esc((p.proposal_sha256||"").slice(0,16))}&hellip;</span>
      · config <span class="mono">${esc((p.config_hash||"").slice(0,12))}&hellip;</span>
      · index <b class="num">${fmtInt((p.index||{}).files)}</b> files at journal <b class="num">${fmtInt((p.index||{}).journal_pos)}</b>
      · critics: ${esc(llm.chain || "disabled")}
      (${fmtInt(llm.critic_items)} judged, ${fmtInt(llm.hinted)} hinted, ${fmtInt(llm.unsure)} unsure${llm.capped ? ", " + fmtInt(llm.capped) + " capped" : ""})</p>
    <p class="hint">Sections execute in dependency order. Everything starts <b>rejected</b> — approve what you reviewed.
      Approvals are hash-bound to this exact proposal.</p>
    <div class="row">
      <button class="btn" data-act="bulk-approve">Approve all ready + gated</button>
      <button class="btn ghost" data-act="bulk-reject">Reset all to reject</button>
    </div></div>`;
  let h = order.map(sid => sectionCard(secs[sid])).join("");

  // the human residue queue
  const rv = p.review || {};
  const counts = rv.counts || {};
  const unsure = rv.unsure_relpaths || [];
  h += `<div class="card"><h3>Review queue — the honest residue</h3>
    <p class="hint">Files no rule and no critic would place. They stay exactly where they are; nothing below executes.</p>
    <div class="row" style="margin:0 0 8px">${Object.entries(counts).map(([q,n]) =>
      chip(q + ": " + fmtInt(n), "out")).join(" ")}
      ${chip("unsure: " + fmtInt(unsure.length), unsure.length ? "warn" : "dim")}</div>
    ${unsure.length ? `<details data-open="queue"><summary>show ${fmtInt(unsure.length)} paths</summary>
      <div class="queue-list">${unsure.slice(0,500).map(u => `<div>${esc(u)}</div>`).join("")}
      ${unsure.length>500 ? `<div>&hellip;+${fmtInt(unsure.length-500)} more</div>` : ""}</div></details>` : ""}
  </div>`;

  // staging / disposal preview
  const stg = Object.entries(p.staging_preview || {});
  h += `<div class="card"><h3>Staging preview (dedup sections)</h3>`;
  h += stg.length
    ? `<div class="scroll"><table class="tbl"><thead><tr><th>staging drive/root</th><th class="num">files</th><th class="num">bytes</th></tr></thead>
       <tbody>${stg.map(([root,v]) => `<tr><td class="mono">${esc(root)}</td>
         <td class="num">${fmtInt(v.files)}</td><td class="num">${fmtBytes(v.bytes)}</td></tr>`).join("")}</tbody></table></div>`
    : `<p class="hint">no staging rows in this proposal</p>`;
  h += `<p class="never" style="margin:8px 0 0">mlo never deletes - disposal of staged files stays yours</p></div>`;

  $("#revBody").innerHTML = h;
  // restore <details> open state across re-renders
  $$("#revBody details").forEach(d => {
    const k = d.dataset.open;
    if(k && OPEN.has(k)) d.open = true;
    d.addEventListener("toggle", () => { if(d.open) OPEN.add(k); else OPEN.delete(k); });
  });
  renderTotals();
}

function renderTotals(){
  const t = approvedTotals();
  $("#revFoot").hidden = !PROPOSAL;
  $("#revTotals").innerHTML = `Approved: <b>${fmtInt(t.sections)}</b> sections ·
    <b>${fmtInt(t.rows)}</b> rows · <b>${fmtBytes(t.bytes)}</b>`;
  $("#revExec").disabled = t.sections === 0;
}

// one delegated handler for every review action (survives re-renders)
$("#tab-review").addEventListener("click", ev => {
  const el = ev.target.closest("[data-act]");
  if(!el) return;
  const act = el.dataset.act;
  if(act==="bulk-approve") return bulk("approve");
  if(act==="bulk-reject")  return bulk("reject");
  const seg = el.closest(".seg");
  if(act==="sec-approve") return setSection(seg.dataset.sid, "approve");
  if(act==="sec-reject")  return setSection(seg.dataset.sid, "reject");
  if(act==="cl-approve")  return setCluster(seg.dataset.sid, seg.dataset.cid, "approve");
  if(act==="cl-reject")   return setCluster(seg.dataset.sid, seg.dataset.cid, "reject");
  if(act==="rows")        return openRows(el.dataset.sid, el.dataset.cid || null);
});

// ── modal plumbing ───────────────────────────────────────────────────────────
function openModal(html){ $("#modal").innerHTML = html; $("#overlay").hidden = false; }
function closeModal(){ $("#overlay").hidden = true; $("#modal").innerHTML = ""; }
$("#overlay").addEventListener("click", ev => { if(ev.target.id==="overlay") closeModal(); });
document.addEventListener("keydown", ev => { if(ev.key==="Escape") closeModal(); });

// ── row drill-down: plan rows + review signals + critic answers ──────────────
let ROWSCTX = null;
async function openRows(sid, cid){
  ROWSCTX = {sid, cid, offset:0, limit:50};
  await fetchRows();
}
async function fetchRows(){
  const {sid, cid, offset, limit} = ROWSCTX;
  const q = new URLSearchParams({section:sid, offset:String(offset), limit:String(limit)});
  if(cid) q.set("cluster", cid);
  if(PROPOSAL && PROPOSAL.run) q.set("run", PROPOSAL.run);
  const r = await api("/api/proposal/rows?" + q.toString());
  if(!r.ok){ openModal(`<h2>Rows</h2>${banner(r.error,"err")}
    <div class="row"><button class="btn" onclick="closeModal()">Close</button></div>`); return; }
  const rows = r.rows.map(renderRow).join("");
  const from = r.total ? r.offset + 1 : 0, to = r.offset + r.rows.length;
  openModal(`<h2>${esc(sid)}${cid ? ` · <span class="mono">${esc(cid)}</span>` : ""}</h2>
    <p class="hint">${r.note ? esc(r.note) : "src &rarr; dst pairs from the sealed plan, joined with the review signals the critics saw."}</p>
    ${r.signals_note ? banner(r.signals_note, "warn") : ""}
    ${rows || `<p class="hint">no rows</p>`}
    <div class="pager">
      <button class="btn sm" onclick="pageRows(-1)" ${r.offset<=0 ? "disabled" : ""}>&larr; prev</button>
      <span class="num">${fmtInt(from)}&ndash;${fmtInt(to)} of ${fmtInt(r.total)}</span>
      <button class="btn sm" onclick="pageRows(1)" ${to>=r.total ? "disabled" : ""}>next &rarr;</button>
      <button class="btn sm ghost" style="margin-left:auto" onclick="closeModal()">Close</button>
    </div>`);
}
function pageRows(dir){
  ROWSCTX.offset = Math.max(0, ROWSCTX.offset + dir*ROWSCTX.limit);
  fetchRows();
}
function renderRow(r){
  let sig = "";
  if(r.signals){
    const s = r.signals, bits = [];
    if(s.mtime) bits.push(`<b>mtime</b> ${esc(s.mtime)}`);
    if(s.bucket) bits.push(`<b>bucket</b> ${esc(s.bucket)}`);
    if(s.language_guess) bits.push(`<b>language</b> ${esc(s.language_guess)}`);
    if(s.origin) bits.push(`<b>origin</b> <span class="mono">${esc(s.origin)}</span>`);
    let html = bits.join(" &nbsp;·&nbsp; ");
    if(s.siblings && s.siblings.length)
      html += `<div style="margin-top:4px"><b>siblings</b> ${s.siblings.slice(0,8).map(x =>
        `<span class="tag">${esc(x)}</span>`).join("")}${s.siblings.length>8 ? " &hellip;" : ""}</div>`;
    if(s.doc_props)
      html += `<div style="margin-top:4px"><b>doc properties</b> ${Object.entries(s.doc_props).map(([k,v]) =>
        `<span class="tag">${esc(k)}: ${esc(v)}</span>`).join("")}</div>`;
    if(html) sig = `<div class="sig">${html}</div>`;
  }
  let critic = "";
  if(r.critic){
    const c = r.critic;
    const conf = (c.confidence!=null) ? Math.round(c.confidence*100) + "%" : "–";
    critic = `<div class="critic"><b>critic:</b>
      ${c.proposed_home ? `<span class="mono">${esc(c.proposed_home)}</span>` : "(no home)"} ·
      <span class="conf">${esc(conf)}</span>
      ${c.rationale ? `<div class="hint" style="margin:3px 0 0">${esc(c.rationale)}</div>` : ""}</div>`;
  }
  return `<div class="rowitem">
    <div class="rowpath">${esc(r.src)}<br><span class="arr">&rarr;</span> ${esc(r.dst)}</div>
    <div class="rowmeta">${chip(r.kind, "out")}
      <span class="num">${fmtBytes(r.size)}</span>
      <span class="mono">${esc(r.rule)}</span></div>
    ${sig}${critic}</div>`;
}

// ── execute modal: typed confirmation, then Pass 2 ───────────────────────────
function buildApprovals(){
  const decisions = {};
  for(const s of PROPOSAL.sections){
    if(!approvable(s)) continue;
    const d = DECISIONS[s.id];
    const ov = Object.entries(d.clusters);
    decisions[s.id] = ov.length
      ? {default: d.default, clusters: Object.fromEntries(ov)}
      : d.default;
  }
  return {schema:"mlo.approvals/1", proposal_sha256:PROPOSAL.proposal_sha256,
          decisions, converge:true};
}

$("#revExec").onclick = () => {
  const t = approvedTotals();
  const lines = [];
  for(const s of PROPOSAL.sections){
    if(!approvable(s)) continue;
    const st = sectionState(s);
    if(st==="reject") continue;
    let rows = s.n_rows, bytes = s.bytes;
    if(st==="mixed"){
      rows = 0; bytes = 0;
      for(const c of s.clusters) if(effCluster(s.id, c.id)==="approve"){ rows+=c.n_rows; bytes+=c.bytes; }
    }
    lines.push(`<tr><td class="mono">${esc(s.id)}</td>
      <td>${statusChip(s.status)}${st==="mixed" ? ` <span class="mixtag">partial</span>` : ""}</td>
      <td class="num">${fmtInt(rows)}${s.status==="gated" ? "*" : ""}</td>
      <td class="num">${fmtBytes(bytes)}</td></tr>`);
  }
  openModal(`<h2>Execute the approved sections</h2>
    <p class="hint">Pass 2 runs these in dependency order with bounded convergence, then verifies.
      Approvals are hash-bound to proposal <span class="mono">${esc((PROPOSAL.proposal_sha256||"").slice(0,16))}&hellip;</span></p>
    <div class="scroll"><table class="tbl">
      <thead><tr><th>section</th><th>status</th><th class="num">rows</th><th class="num">bytes</th></tr></thead>
      <tbody>${lines.join("")}</tbody></table></div>
    ${t.gatedRows ? `<p class="hint">* gated sections build in Pass 2; their row counts are verdict previews.</p>` : ""}
    <p class="hint"><b>${fmtInt(t.rows)}</b> rows across <b>${fmtInt(t.sections)}</b> sections ·
      <b>${fmtBytes(t.bytes)}</b>. Duplicates are staged, never deleted.</p>
    <label>Type the row count (<b class="num">${fmtInt(t.rows)}</b>) or the word EXECUTE to arm</label>
    <input type="text" id="armInput" autocomplete="off" spellcheck="false">
    <div class="row">
      <button class="btn danger" id="armGo" disabled>Execute now</button>
      <button class="btn ghost" onclick="closeModal()">Cancel</button>
      <span id="armNote" class="hint"></span>
    </div>`);
  const input = $("#armInput"), go = $("#armGo");
  const want = String(t.rows);
  input.oninput = () => {
    const v = input.value.trim();
    go.disabled = !(v === want || v.toUpperCase() === "EXECUTE");
  };
  input.focus();
  go.onclick = async () => {
    go.disabled = true;
    const r = await api("/api/pilot/execute", {run: PROPOSAL.run, approvals: buildApprovals()});
    if(!r.ok){ $("#armNote").innerHTML = banner(r.error, "err"); go.disabled = false; return; }
    closeModal();
    showTab("run");
    $("#runOut").innerHTML = "";
    pollJob();
  };
};

// ── execute progress + final report ──────────────────────────────────────────
function renderExecProgress(j){
  const box = $("#runPhases");
  const evs = j.events || [];
  const items = evs.map((e, i) => {
    const last = i === evs.length-1;
    const cls = j.finished ? (j.error && last ? "err" : "done") : (last ? "active" : "done");
    const label = e.phase === "execute" ? "Execute" : e.phase === "verify" ? "Verify" : e.phase;
    const info = Object.entries(e.info||{}).map(([k,v]) => k+"="+v).join("  ");
    return `<li class="${cls}"><span class="dot"></span>
      <span class="pname">${esc(label)}</span><span class="pinfo">${esc(info)}</span></li>`;
  }).join("");
  box.innerHTML = `<ol class="phases">${items ||
    `<li class="active"><span class="dot"></span><span class="pname">starting&hellip;</span></li>`}</ol>`;
}

function renderExecReport(j){
  const out = $("#runOut");
  if(j.error){ out.innerHTML = `<div class="card"><h3>Execution refused / failed</h3>${banner(j.error, "err")}</div>`; return; }
  const r = j.result || {};
  const v = r.verify || {}; const lib = v.library || {}; const stg = v.staging || {};
  let h = `<div class="card"><h3>Section outcomes</h3>` +
    (r.outcomes||[]).map(o => `<div class="verdictline">
      <span class="sid">${esc(o.id)}</span> ${statusChip(o.status)}
      ${o.detail ? `<span class="hint" style="margin:0">${esc(o.detail)}</span>` : ""}
      <span class="d">${fmtInt(o.done)} done · ${fmtInt(o.cycles)} cycle${o.cycles===1?"":"s"} · ${fmtInt(o.drift)} drift${o.unconverged_rows ? " · " + fmtInt(o.unconverged_rows) + " unconverged" : ""}${o.rejected_dropped ? " · " + fmtInt(o.rejected_dropped) + " rejected-dropped" : ""}</span>
    </div>`).join("") + `</div>`;

  h += `<div class="card"><h3>Verify</h3>`;
  if(v.blocking) h += banner("BLOCKING: protected content found in a staging root - resolve it before any disposal.", "err");
  h += `<div class="row" style="margin:4px 0 0">
    ${chip("unindexed: " + fmtInt(lib.unindexed), lib.unindexed ? "warn" : "dim")}
    ${chip("missing: " + fmtInt(lib.missing), lib.missing ? "warn" : "dim")}
    ${chip("drifted: " + fmtInt(lib.drifted), lib.drifted ? "warn" : "dim")}
    ${chip("mlopart: " + fmtInt(lib.mlopart), lib.mlopart ? "warn" : "dim")}
    ${chip("protected in staging: " + fmtInt(stg.protected_in_staging), stg.protected_in_staging ? "bad" : "dim")}
    ${chip("unjournaled staging: " + fmtInt(stg.unjournaled), stg.unjournaled ? "warn" : "dim")}
  </div></div>`;

  const staged = Object.entries(r.staging || {});
  h += `<div class="card"><h3>Staged for disposal</h3>`;
  h += staged.length
    ? staged.map(([sid, s]) => `<div class="verdictline"><span class="sid">${esc(sid)}</span>
        <span class="d">${fmtInt(s.staged)} files staged</span></div>`).join("")
    : `<p class="hint">nothing staged this run</p>`;
  h += `<p class="never" style="margin:8px 0 0">mlo never deletes - disposal of staged files stays yours</p></div>`;

  const unsure = PROPOSAL && PROPOSAL.review ? (PROPOSAL.review.unsure_relpaths||[]).length : null;
  h += `<div class="card"><h3>What remains</h3>
    ${unsure!=null ? `<p class="hint">Human review queue: <b class="num">${fmtInt(unsure)}</b> files (Review tab &rarr; Review queue) — they stayed put.</p>` : ""}
    <p class="hint">Full machine-readable report:</p>
    <div class="samp">${esc(r.summary_path||"")}</div>
    ${r.exit_code===0 ? banner("Converged clean - exit 0.", "okay")
      : banner("Completed with residue or findings - exit " + r.exit_code + ". Re-run mlo pilot to re-analyze what remains.", "warn")}
  </div>`;
  out.innerHTML = h;
  renderHome();   // journal moved; refresh the dashboard numbers
}

// ── Guided mode: the original v1 stepper, verbatim flow ──────────────────────
let PLAN = null;   // {plan_path,...} once built

function step(n, title, hint) {
  const el = document.createElement("section");
  el.className = "step"; el.id = "step"+n;
  el.innerHTML = `<h2><span class="stepnum">${n}</span>${esc(title)}</h2>
    <div class="hint">${hint}</div><div class="body"></div><div class="out"></div>`;
  $("#guided").appendChild(el);
  return el;
}
function activate(n){ const e=$("#step"+n); e.className="step active"; e.scrollIntoView({behavior:"smooth",block:"center"}); }
function markDone(n){ $("#step"+n).classList.remove("active"); $("#step"+n).classList.add("done"); }
function showErr(el, msg){ el.innerHTML = `<div class="err">&#9888; ${esc(msg)}</div>`; }
function pills(obj, order){
  const keys = order || Object.keys(obj);
  return `<div class="pills">${keys.map(k=>
    `<div class="pill"><b>${obj[k]??0}</b><span>${esc(k)}</span></div>`).join("")}</div>`;
}
function busy(btn, on, label){ btn.disabled=on; if(on){btn.dataset.t=btn.textContent; btn.textContent=label||"working…";}
  else if(btn.dataset.t){btn.textContent=btn.dataset.t;} }

function renderTree(node, depth=0){
  const pad = "  ".repeat(depth);
  const label = node.name+"/";
  let out = `<div class="tnode">${esc(pad+label)} <span class="tcount">(${node.count})</span></div>`;
  for(const d of node.dirs||[]) out += renderTree(d, depth+1);
  if(node.more) out += `<div class="tnode tcount">${esc("  ".repeat(depth+1))}…+${node.more} more folders</div>`;
  return out;
}

// Step 1: setup
const s1 = step(1, "Your folders", "The messy source and the organized library it should land in.");
s1.querySelector(".body").innerHTML = `
  <label>Messy folder (the source to organize)</label>
  <input type="text" id="src" placeholder="E:\ or D:\phone-backup">
  <label>Library folder (where organized files live)</label>
  <input type="text" id="lib" placeholder="F:\Organized">
  <label>Name for this source</label>
  <input type="text" id="name" placeholder="old-drive" value="old-drive">
  <div class="row"><button class="btn primary" id="setupBtn">Save &amp; check &rarr;</button></div>`;
$("#setupBtn").onclick = async () => {
  const out = s1.querySelector(".out"), btn=$("#setupBtn");
  busy(btn, true, "checking…");
  const r = await api("/api/setup", {
    library_root: $("#lib").value.trim(), source_root: $("#src").value.trim(),
    source_name: $("#name").value.trim() });
  busy(btn, false);
  if(!r.ok){ showErr(out, r.error); return; }
  out.innerHTML = `<div class="ok">&#10003; Ready. Library: <b>${esc(r.library_root)}</b> · source "<b>${esc(r.source_name)}</b>": ${esc(r.source_root)}</div>`;
  markDone(1); activate(2);
};

// Step 2: scan
const s2 = step(2, "Scan", "Fingerprint both folders so mlo knows what you have. Read-only — nothing moves.");
s2.querySelector(".body").innerHTML = `<div class="row"><button class="btn primary" id="scanBtn">Scan now</button></div>`;
$("#scanBtn").onclick = async () => {
  const out = s2.querySelector(".out"), btn=$("#scanBtn");
  busy(btn, true, "scanning library…");
  let r = await api("/api/scan", {target:"library"});
  if(!r.ok){ busy(btn,false); showErr(out, r.error); return; }
  const lib = r.count;
  busy(btn, true, "scanning your mess…");
  r = await api("/api/scan", {target:"source"});
  busy(btn, false);
  if(!r.ok){ showErr(out, r.error); return; }
  out.innerHTML = pills({"library files":lib, "source files":r.count, "unreadable":r.unreadable});
  out.innerHTML += `<div class="row"><button class="btn primary" id="scanNext">Continue &rarr;</button></div>`;
  $("#scanNext").onclick = () => { markDone(2); activate(3); };
};

// Step 3: sort
const s3 = step(3, "Sort", "Every source file is classified against your library.");
s3.querySelector(".body").innerHTML = `
  <div class="hint">
   <b>UNIQUE</b> = new, will be organized in · <b>ORGANIZED</b> = already in your library ·
   <b>JUNK</b> = temp/thumbnail files · <b>REVIEW</b> = needs a look (left untouched).</div>
  <div class="row"><button class="btn primary" id="vBtn">Classify</button></div>`;
$("#vBtn").onclick = async () => {
  const out = s3.querySelector(".out"), btn=$("#vBtn");
  busy(btn, true, "classifying…");
  const r = await api("/api/verdicts", {});
  busy(btn, false);
  if(!r.ok){ showErr(out, r.error); return; }
  out.innerHTML = pills(r.counts, ["UNIQUE","ORGANIZED","JUNK","REVIEW"]);
  const uniq = r.counts.UNIQUE||0;
  out.innerHTML += uniq
    ? `<div class="row"><button class="btn primary" id="vNext">Build the plan &rarr;</button></div>`
    : `<div class="note">No new (UNIQUE) files to organize — nothing to move.</div>`;
  const nx = $("#vNext"); if(nx) nx.onclick = () => { markDone(3); activate(4); buildPlan(); };
};

// Step 4: preview the final structure
const s4 = step(4, "Preview the organized structure", "Exactly which folders and files this will create in your library. Nothing has moved yet.");
async function buildPlan(){
  const out = s4.querySelector(".out");
  out.innerHTML = `<div class="note">building plan…</div>`;
  const r = await api("/api/plan", {});
  if(!r.ok){ showErr(out, r.error); return; }
  PLAN = r;
  let html = `<div class="pills"><div class="pill"><b>${r.n_rows}</b><span>files to organize</span></div></div>`;
  html += r.notes.map(n=>`<div class="note">&bull; ${esc(n)}</div>`).join("");
  html += `<div class="hint" style="margin-top:12px">New library structure this will create:</div>`;
  html += `<div class="tree">${renderTree(r.tree)}</div>`;
  if(r.sample.length){
    html += `<div class="hint" style="margin-top:12px">Sample moves:</div><div class="samples">`;
    html += r.sample.map(m=>`${esc(m.src)}<br>&nbsp;&nbsp;&rarr; ${esc(m.dst)}`).join("<br>");
    html += `</div>`;
  }
  html += `<div class="row"><button class="btn primary" id="pNext" ${r.n_rows?"":"disabled"}>Approve this structure &rarr;</button></div>`;
  out.innerHTML = html;
  const nx=$("#pNext"); if(nx) nx.onclick = () => { markDone(4); activate(5); };
}

// Step 5: rehearse
const s5 = step(5, "Rehearse (dry-run)", "A full dry-run of every move with no files touched — the last check before committing.");
s5.querySelector(".body").innerHTML = `<div class="row"><button class="btn primary" id="rBtn">Rehearse</button></div>`;
$("#rBtn").onclick = async () => {
  const out = s5.querySelector(".out"), btn=$("#rBtn");
  busy(btn, true, "rehearsing…");
  const r = await api("/api/apply", {plan_path:PLAN.plan_path, execute:false});
  busy(btn, false);
  if(!r.ok){ showErr(out, r.error); return; }
  out.innerHTML = pills(r.counts);
  out.innerHTML += `<div class="row"><button class="btn primary" id="rNext">Looks right &rarr;</button></div>`;
  $("#rNext").onclick = () => { markDone(5); activate(6); };
};

// Step 6: execute
const s6 = step(6, "Organize for real", "Copies your UNIQUE files into the library (originals are kept — mlo never deletes).");
s6.querySelector(".body").innerHTML = `<div class="row"><button class="btn danger" id="xBtn">Execute — organize now</button></div>`;
$("#xBtn").onclick = async () => {
  const out = s6.querySelector(".out"), btn=$("#xBtn");
  if(!confirm("Copy "+PLAN.n_rows+" files into your library now?")) return;
  busy(btn, true, "organizing…");
  const r = await api("/api/apply", {plan_path:PLAN.plan_path, execute:true});
  busy(btn, false);
  if(!r.ok){ showErr(out, r.error); return; }
  out.innerHTML = pills(r.counts);
  if(r.residual) out.innerHTML += `<div class="note">Some rows didn't complete (drift) — a residual plan was written; re-run to retry.</div>`;
  out.innerHTML += `<div class="row"><button class="btn primary" id="xNext">Verify &rarr;</button></div>`;
  $("#xNext").onclick = () => { markDone(6); activate(7); };
};

// Step 7: verify
const s7 = step(7, "Verify", "Confirm the library on disk matches mlo's record.");
s7.querySelector(".body").innerHTML = `<div class="row"><button class="btn primary" id="verBtn">Verify library</button></div>`;
$("#verBtn").onclick = async () => {
  const out = s7.querySelector(".out"), btn=$("#verBtn");
  busy(btn, true, "verifying…");
  const r = await api("/api/verify", {});
  busy(btn, false);
  if(!r.ok){ showErr(out, r.error); return; }
  out.innerHTML = pills(r.counts);
  out.innerHTML += r.blocking
    ? `<div class="err">&#9888; Protected content found in staging — resolve before any disposal.</div>`
    : `<div class="ok">&#10003; All done. Your mess is organized.</div>`;
  markDone(7);
};

// ── boot: dashboard, analyze form, guided prefill, resume a live job ─────────
(async () => {
  await renderHome();
  fillAnalyzeForm();
  const st = STATE || {};
  if(st.config_exists && st.library_root && st.source_root){
    $("#lib").value = st.library_root; $("#src").value = st.source_root;
    if(st.source_name) $("#name").value = st.source_name;
  }
  activate(1);
  const js = await api("/api/pilot/status");   // page reloaded mid-job? resume
  if(js.job && !js.job.finished){
    showTab(js.job.kind === "analyze" ? "analyze" : "run");
    if(js.job.kind === "analyze") $("#anProgressCard").hidden = false;
    pollJob();
  }
})();
</script>
</body>
</html>
"""
