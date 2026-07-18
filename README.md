# mlo — media library organizer

[![ci](https://github.com/msampath/library-organizer/actions/workflows/ci.yml/badge.svg)](https://github.com/msampath/library-organizer/actions/workflows/ci.yml)
[![license: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![python: 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**A safety-first consolidation engine for personal data hoards, with an agent layer
designed so even a small local model does trustworthy work.**

`mlo` consolidates years of scattered files — old drives, phone backups, NAS dumps,
recovery-tool output — into one organized, deduplicated library. It was distilled from a
real 2 TB / 800,000-file consolidation whose first pipeline needed five rewrites; this
engine is the re-architecture that makes each of those failure classes *structurally
impossible*, not just guarded by discipline.

> Status: v0.2 — the engine, the agent layer, and content-derived organization
> (Jellyfin-style `Movies/Title (Year)`, `Series/Season NN`, photo year folders) are in,
> including in-place library repair (`plan reorganize`). See
> [docs/roadmap.md](docs/roadmap.md).

## The trust model

Most "AI file organizers" put the model in charge and hope. `mlo` inverts it:

1. **Code enforces safety.** Every filesystem mutation flows through one small kernel
   that knows protected paths, same-drive staging, never-overwrite — and there is **no
   delete API anywhere in the codebase** (AST-enforced in CI). The one delete-adjacent
   capability, `mlo dispose` (C68), hands staging-only, journal-verified files to the
   **OS's own Recycle Bin / trash** — recoverable, staging-scoped, and gated behind a
   typed row-count confirmation. The engine cannot destroy your data, no matter what
   the model says.
2. **The model supplies judgment, inside a protocol.** Classification and triage happen
   through bounded tasks: schema-validated outputs, enumerated choices, abstention
   (`UNSURE`) as a first-class answer, escalation local → cloud → human. Designed and
   evaluated for **gpt-oss-20b running on your own machine** — your filenames never have
   to leave your house.
3. **Humans gate irreversibility.** Staging is reversible by design; disposal of staged
   duplicates happens only through `mlo dispose` — a plan you review, executed only with
   an explicit `--confirm-dispose <row count>`, into the OS trash (never a true delete).

## Why another organizer

Because the five-rewrite pipeline this replaces kept a defect ledger, and every entry is
now a mechanism with a named regression test — see
[docs/defect-ledger.md](docs/defect-ledger.md):

- a phase that ran twice created 165K duplicate files → **journaled, content-addressed
  operations; re-running anything is a no-op**
- one missing keyword silently misrouted an entire language → **classifiers must prove
  coverage; unmatched files block the plan and name themselves**
- stale scan CSVs were consulted as truth, twice → **derived artifacts carry freshness
  stamps; the engine refuses stale inputs and never reads CSVs back**
- files manually moved mid-pipeline stranded 3,858 unique files in a staging folder →
  **plans re-verify every row's preconditions at execute time; drift is a report, not a loss**
- `shutil.copy2` was trusted blindly → **the kernel re-hashes every copy before journaling it**

## Quickstart

**The 2-pass cleanup** (the front door). Pass 1 analyzes everything read-only and
rehearsed into ONE sealed proposal; Pass 2 is a single review-and-execute sitting in a
localhost web UI. Approvals are hash-bound to exactly what you reviewed; execution
auto-converges (bounded) and verifies; the engine still deletes nothing — staged
duplicates leave only via `mlo dispose` into the OS Recycle Bin / trash, behind a typed
row-count confirmation.

```console
$ mlo pilot                       # Pass 1: analyze everything -> proposal.json
$ mlo serve                       # Pass 2: review clusters -> approve -> Execute
                                  #   (http://127.0.0.1:8765, Ctrl-C to stop)
```

Headless Pass 2: `mlo pilot --execute --proposal <proposal.json> --approve-all`.

`mlo serve` also keeps the guided single-source stepper (scan → sort → structure
preview → dry-run → organize) for first contact with one messy folder.

Or the granular CLI:

```console
$ pip install mlo                 # (not yet published — build from source for now)
$ mlo init                        # writes an annotated mlo.toml
$ mlo check                       # validates config + root reachability
$ mlo scan library                # fingerprint your library into the index
$ mlo scan old-drive              # fingerprint a source
$ mlo verdicts old-drive          # ORGANIZED / JUNK / UNIQUE / REVIEW
$ mlo plan organize old-drive     # uniques -> library (copy-before-stage is enforced)
$ mlo apply ".mlo/plans/plan-organize-old-drive-ab12cd34.jsonl"            # rehearse
$ mlo apply ".mlo/plans/plan-organize-old-drive-ab12cd34.jsonl" --execute
$ mlo plan dedup old-drive        # duplicates + junk -> staging (same-drive, reversible)
$ mlo apply ".mlo/plans/plan-dedup-old-drive-9f00aa11.jsonl" --execute
$ mlo verify library
$ mlo plan reorganize --under Video/old-drive --exif   # repair flat dumps in place;
                                                       # correct trees never move
```

Agent layer (local model via Ollama, or any OpenAI-compatible endpoint):

```console
$ mlo agent triage old-drive     # LLM recommends dispositions for the REVIEW pile
$ mlo agent classify old-drive   # labels the tail your rules didn't cover
$ mlo agent eval --mock          # harness self-check, no model needed
$ mlo agent eval                 # measures the configured chain on the golden sets
```

## Design documents

| Doc | What it covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | SQLite-as-truth store, safety kernel, plan/apply contract, freshness |
| [docs/defect-ledger.md](docs/defect-ledger.md) | every real-world failure → the mechanism that kills it → its regression test |
| [docs/agent-design.md](docs/agent-design.md) | the small-model-first protocol, fallback chain, eval results |
| [docs/lessons-learned.md](docs/lessons-learned.md) | the five-pipeline story this project distills |
| [docs/formats.md](docs/formats.md) | plan.jsonl / summary.json schemas, exit codes |
| [docs/runbook.md](docs/runbook.md) | end-to-end consolidation walkthrough |
| [docs/web-ui.md](docs/web-ui.md) | the `mlo serve` 2-pass UI — what shipped, and what stays CLI/human |

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
