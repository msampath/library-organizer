# The web UI (`mlo serve`) — status & plan

## Context

The engine was CLI-only. The common real-world ask — *"here's my disorganized
mess, here's where I want it, make it happen"* — is intuitive as a UI, not a
sequence of typed commands. `mlo serve` adds a **localhost-only, approval-gated**
front over the *same safe engine*: the web layer performs no filesystem mutation
itself; every step calls the existing `scan` / `verdict` / `plan` / `apply` /
`verify` / `pilot` functions, so the safety kernel (`safeops.py` is the only
mutator, no delete API — enforced by `tests/test_architecture.py`) and the
"execute is a human act" rule hold unchanged. Zero new dependencies (stdlib
`http.server`).

## What shipped (2026-07-10 — the 2-pass product UI, P4)

`mlo serve` → `http://127.0.0.1:8765`. One page, five screens; the 2-pass flow
is the primary path and the v1 stepper stays as "Guided mode".

1. **Overview** — state cards (index count + freshness, journal position,
   sources with per-source scan/verdict freshness, staging roots, newest
   proposal, latest run summary) and the two entry points.
2. **Analyze (Pass 1)** — options form (sources, `--under` prefixes,
   confirm-MiB, critic chain/limit, cross-check) → `pilot.analyze` on the ONE
   background job thread; the page polls `/api/pilot/status` and renders the
   phase checklist (scan → plans → review-set → critics → re-plan → rehearse →
   seal) with per-phase progress info.
3. **Review (the core)** — the sealed proposal: section cards in
   `execution_order` with status chips (ready/gated/blocked/empty), rehearsal
   counts, notes, `blocked_reason` verbatim; three-state approve/reject per
   section with per-cluster overrides; a row drill-down joining each plan row
   with its review-set signals (siblings, doc properties, mtime, origin) and
   the critic's full answer (proposed_home, confidence, rationale); the human
   review queue (`unsure_relpaths` — the honest residue); and the staging /
   disposal preview ("mlo never deletes - disposal of staged files stays
   yours").
4. **Execute (Pass 2)** — a typed-confirmation modal (type the approved row
   count or `EXECUTE`); approvals are persisted to the run directory FIRST
   (audit), then `pilot.execute` runs hash-bound to the reviewed proposal
   (`proposal_sha256`, ledger C25); progress + a final report: per-section
   outcome chips (converged/residual/rejected/blocked) with cycles and drift,
   the verify panel (blocking findings are a red banner), staged-for-disposal
   counts, and the `summary.json` path.
5. **Guided mode** — the v1 single-source stepper, unchanged flow
   (setup → scan → sort → structure preview → rehearse → execute → verify).

Routes (JSON, localhost-only): `GET /api/state`, `POST /api/pilot/analyze`,
`GET /api/pilot/status`, `GET /api/proposal[?run=]`,
`GET /api/proposal/rows?section=&cluster=&offset=&limit=`,
`POST /api/pilot/execute`, `GET /api/runs/latest/summary`, plus the v1 step
routes. One job at a time: a second submission is refused
(`{"error": "a job is already running"}`), never queued — the engine is a
single-mutator design. The worker thread opens its own `Store` (per-thread
sqlite) and every write goes through `report` helpers, so the architecture AST
test holds for `web.py` unchanged.

Display vs identity: paths in responses are display-only (`_disp`, lossy for
lone surrogates); every actionable identifier — section/cluster/op ids and the
proposal seal — round-trips losslessly, because approvals bind by exact ids.

Code: `src/mlo/web.py` (pure `act_*` functions + the job runner + threaded
server + embedded single-page app), `src/mlo/cli.py` (`serve` subcommand).
Tests: `tests/test_web.py` — the v1 pipeline, the analyze job lifecycle
(submit → poll → sealed proposal, journal provably untouched), double-job
refusal, tampered-proposal refusal, rows pagination/cluster-filter/critic-join
(scripted critic), execute-through-web (files move, approvals audited,
summary served), malformed/stale approvals refusals, and a real-HTTP smoke
test on an ephemeral port.

## What the critics see in the UI

The row drill-down surfaces the CANONICAL full-signal review (owner rule,
2026-07-09): every joined row shows the same signals the critic judged with —
full path, siblings, embedded document properties, dates, origin — plus the
critic's `proposed_home`, `confidence`, and `rationale`. Abstentions
(`unsure_relpaths`) are a first-class queue in the review screen, never
defaulted to an action.

## Out of scope (stays CLI / stays human)

Disposal of staged files is not an engine capability, so it is not a UI
capability either — the staging panel says exactly that. `agent eval`, `mlo
export`, and the operator toolkit remain CLI-only.

## Verification

- `python -m pytest tests/test_web.py` — 13 tests, all through the same
  handlers the browser calls (a real `mlo.toml` on disk; no injected configs).
- Live check (done 2026-07-10): `mlo serve` against a scratch world — analyze
  → review (clusters, rows, signals) → approve-all → typed confirmation →
  execute → converged report, then the on-disk tree matched the report
  (library organized, originals staged, nothing deleted).
