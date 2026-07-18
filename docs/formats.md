# Formats — plan files, run summaries, CSV views, exit codes

This is the reference for every machine-readable artifact `mlo` produces. Two rules
govern all of them:

1. **Schema versions are semver events.** Every artifact carries a `schema` field
   (`mlo.plan/1`, `mlo.summary/1`). A breaking change bumps the number and the package
   minor version; consumers must check it.
2. **`summary.json` (plus its `suggested_next` field) is the agent interface.** Anything
   that drives `mlo` — the built-in agent layer, an external LLM agent, a shell script —
   reads the summary and picks from the suggested commands. There is no hidden channel.

---

## Plan files (`.mlo/plans/plan-<kind>-<source>-<hash8>.jsonl`) — schema `mlo.plan/1`

A plan is a **hash-stamped JSONL artifact**: one JSON object per line, so it streams and
greps at any size. The filename carries the kind, the (sanitized) source name, and the
first 8 hex chars of the plan_id. Three parts:

**Header (line 1)** — provenance and freshness stamps of every input:

```json
{"schema": "mlo.plan/1", "kind": "dedup", "source": "old-drive",
 "created": "2026-07-06T02:41:09", "config_hash": "9c41f0e2a77b...",
 "inputs": [{"artifact_id": "scan:old-drive", "journal_pos": 41822},
            {"artifact_id": "index:library", "journal_pos": 41822},
            {"artifact_id": "verdicts:old-drive", "journal_pos": 41822}],
 "tool_version": "0.1.0"}
```

**Body (one row per operation)** — everything apply needs, fixed at plan time:

```json
{"op_id": "5f0c9d2ab4e1...", "kind": "stage_move",
 "src": "E:\\old-drive\\photos\\IMG_2231.jpg", "dst": "E:\\Delete\\old-drive\\photos\\IMG_2231.jpg",
 "pre": {"size": 2841733, "quick_hash": "aa41...:9b02..."},
 "reason": {"verdict": "ORGANIZED", "rule": "fp-match:files.file_id=88123"}}
```

- `op_id` = SHA-256 of the canonical `(kind, src, dst, pre.size, pre.quick_hash)` —
  content-addressed, so a completed op re-applies as a no-op forever.
- `kind` ∈ `stage_move | copy_in | move_within | rmdir_empty | dispose`. There is
  no true-delete kind; the format cannot express one. `dispose` (C68, the L18
  amendment) journals `src == dst` and sends a staging-only file to the OS
  Recycle Bin / trash; executing a dispose plan additionally requires
  `--confirm-dispose <exact row count>`. Plan kinds map to op kinds:
  `organize` emits `copy_in`, `dedup` emits `stage_move`, `reorganize` (v0.2,
  library-internal restructuring) emits `move_within` with
  `reason: {"verdict": "REORGANIZE", "rule": "route:..."}` rows carrying router
  provenance. `dedup-library` (stage byte-identical duplicate content out of the
  library; full-SHA-256-confirmed, one canonical always stays) and
  `stage-library` (stage an explicit judged list, e.g. triage junk) emit
  `stage_move` whose library-side source rows are deleted from the index in the
  same transaction as the journal `done`. The later library movers map the same
  way: `containers`, `flatten-provenance`, `date-drain` and `relocate` (explicit
  `--map` mover) emit `move_within`; `bad-archives` (integrity-failed archives,
  CLI-only by design — see ledger C37) emits `stage_move`; `prune-empty` emits
  `rmdir_empty`.
- `dst` is final. Apply never invents destination names; an occupied `dst` is drift.
- `reason.rule` records which rule/verdict produced the row — every operation is
  auditable after the fact.

**Footer (last line)** — two seals with two jobs:

```json
{"content_sha256": "e3b0c44298fc1c149afbf4c8996fb924...",
 "plan_id": "7d1a4b90c2ffe6a3...", "rows": 12444}
```

- `plan_id` is **semantic identity**: SHA-256 over the canonical
  `(kind, source, config_hash, inputs, rows)` — the `created` timestamp is
  deliberately excluded, so rebuilding an identical plan yields the identical
  `plan_id` (and `write_plan` just returns the existing file). Executed-status
  preservation and residual convergence both hang on this stability.
- `content_sha256` is **file integrity**: a byte hash over everything before the
  footer. A plan that fails this check does not apply — corrupt or hand-edited
  files are refused, never partially trusted.

A plan whose `plan_id` is already marked executed re-applies as all-`skipped_done`,
exit 0. There is no `--force`.

## Run summaries (`.mlo/runs/<run_id>/summary.json`) — schema `mlo.summary/1`

Written by the two commands whose outcomes drive the loop: **`apply`** (rehearsal and
execute) and **`agent eval`**. Other commands report on stdout and in the store; giving
each of them a summary is roadmap work. Realistic `apply` example (these are the fields
the code emits — nothing aspirational):

```json
{
  "schema": "mlo.summary/1",
  "run": "20260706-024109-a1b2c3",
  "command": "apply --execute",
  "plan_id": "e3b0c44298fc1c149afbf4c8996fb924...",
  "config_hash": "9c41f0e2a77b...",
  "counts": {
    "by_op_state": {"done": 12419, "skipped_drift": 3}
  },
  "drift": [
    {"src": "E:\\old-drive\\notes.txt", "dst": "E:\\Delete\\old-drive\\notes.txt",
     "detail": "size drift (1199 != planned 428)"}
  ],
  "residuals": ["5f0c9d2ab4e1..."],
  "warnings": [],
  "exit_code": 3,
  "suggested_next": [
    {"cmd": "mlo apply \".mlo/plans/plan-dedup-residual-old-drive-77aa01bc.jsonl\" --execute",
     "why": "3 rows did not complete"}
  ]
}
```

- `drift` entries are `{src, dst, detail}` (capped at 200); `residuals` is the list of
  op_ids that joined the residual plan (capped at 500).
- `agent eval` summaries carry a `results` list (the metric dicts) instead of drift/residuals.

`suggested_next` entries are **exact CLI strings**. The built-in orchestrator (and any
external agent) may only choose among them — that enumeration is what makes a small
local model safe to put in the loop.

## JSON sidecars (`.mlo/runs/<run_id>/*.json`) — the reorganize/hints loop (v0.2)

Unlike CSVs, these two ARE meant to be read back by the next command in the chain:

**`unrouted.json`** — written by `mlo plan reorganize` when in-scope *media* files had
no derivable identity (they stay put). A flat JSON list of library relpaths:

```json
["Video\\old\\dash\\FILE001.mp4", "Video\\old\\clips\\untitled.mkv"]
```

**Hints** — written by `mlo agent classify --media --paths <unrouted.json>` (name via
`--out`, default `hints.json`, always inside the run directory; the path is printed).
Consumed by `mlo plan organize|reorganize --hints <file>`. Keys are relpaths; every
field optional; unknown keys per entry are refused (a typo cannot silently drop a hint):

```json
{
 "Video\\old\\dash\\FILE001.mp4": {"media_kind": "personal"},
 "Video\\old\\clips\\untitled.mkv": {"media_kind": "movie", "language": "Tamil", "year": 1992}
}
```

`media_kind` ∈ `movie | tv | personal | music`, or any configured
`[layout.subtypes]` kind (whatsapp, anime, screenshot, …) — the kind itself is
not enum-validated at load, the router simply ignores one with no configured
sub-root; `language` must be a configured language name (or the configured
default); `year` an integer 1900–2035; `content_kind` ∈ `video | audio | image`
(a magic-byte sniff verdict — drives the false-carve holding pen, never the
taxonomy). Hints are advisory identity, never authority: every placement they
influence still becomes an ordinary plan row that passes all build gates and
the apply rehearsal.

`book_author`, `book_title`, `book_series` (P17/C43) — free-text strings or
`null`; `book_index` — integer ≥ 0 or `null` (the position within `book_series`).
Written either by `hints.augment_bookmeta_library` (embedded epub/mobi metadata)
or by a merged Opus-subagent judgment file (§ the Ebooks review protocol,
runbook.md); the router prefers a hint over its own `bookmeta.parse_name` fallback
for any book field the hint actually supplies (`taxonomy.route()`'s `Ebooks`
branch), and the four keys round-trip through `hints.load_hints` like any other
hint — an unknown key is still refused.

## Proposals (`.mlo/runs/<run_id>/proposal.json`) — schema `mlo.proposal/1`

The pilot's consolidated Pass-1 artifact: everything `mlo pilot` analyzed, in one
reviewable, SEALED document. Like a plan it carries an integrity seal —
`proposal_sha256` over the canonical JSON minus the seal field — verified by
`report.read_proposal` before any executor trusts it, so what the human reviewed is
provably what executes.

Top-level fields:

- `sections` — one entry per plan kind that applies: `id` (`organize:<src>`,
  `reorganize:library`, ...), `status` (`ready` — plan built and rehearsed · `gated` —
  built in Pass 2 after its dependency executes (source dedup behind organize, L13) ·
  `blocked` — a build gate refused, remedy in `blocked_reason` · `empty`), `plan_path`
  / `plan_id`, `n_rows`, `bytes`, `rehearsal` (`would_do` / `skipped_done` / `drift`
  counts from the read-only rehearsal), `builder_args` (enough to re-run the builder in
  Pass 2), and `clusters`. For the pure-index movers (containers, reorganize,
  date-drain, flatten-provenance) the sealed plan is the PROJECTED END-STATE across
  Pass-2 convergence, not just the first cycle (C42/P16): Pass 1 replays the whole
  cycle chain against an in-memory index copy so the human approves what actually
  executes. dedup-library/prune-empty aren't index-rehearsable (full-hash / dir-walk)
  and seal their first-cycle plan.
- `clusters` — the reviewable rollup of a section's plan rows: `{id, label, rule,
  n_rows, bytes, op_ids_sha256, sample<=5}`. The plan file stays the row-level truth;
  clusters are a deterministic VIEW (`pilot.cluster_rows`) the executor recomputes from
  the sealed plan and verifies by `op_ids_sha256` — approve-X-execute-X, mechanically.
- `execution_order` — dependency order: source organize -> source dedup ->
  containers -> dedup-library -> reorganize -> date-drain -> flatten-provenance ->
  prune-empty (prune is a preview; Pass 2 rebuilds it after the moves). `containers`
  (C33, D10-D12 semantics) moves semantic units — phone backups, drive images, app
  exports — to their kind's home: phone-backups are DEVICE-KEYED
  (`Backups\Phones\<S5|Nexus6|…>`), files merge by identity across sources,
  byte-identical collisions dedup-skip, content-different clashes get an owner
  discriminator, double-clashes skip-and-report. Once home, the tree is claimed
  forever (C39 — `root_of()` recognizes `<home>\<ident>` paths, so no per-file
  mechanism can tear a consolidated snapshot apart). `flatten-provenance` strips
  device-origin path segments (`E_NAS1`, `G_Phone1`, `HDD2_Part2` …) that
  reorganize cannot touch (non-media buckets return None from `taxonomy.route()`; audio
  hits are C19-blocked); every intermediate segment is checked at any depth (C34),
  C21 twin guard, L17 collision skip, a
  containers skip (a backup-named wrapper is a snapshot, not a dump), and its
  `exclude_srcs` set prevents same-src double-planning with the earlier movers this run.
  **C47 relaxation:** a provenance segment sitting DEEPER inside one of the curated
  layout roots (`music_root/<genre>/<PROV>/…`, `photos_root/<year>/<PROV>/…`) is
  stripped too — the file is already triaged into that subtree and the device
  folder is a proven interloper; the C28 boundary itself (a provenance segment
  that is the media-bucket TOP's own direct child, e.g. `Audio\I_SSD1\…`)
  still holds untouched. `personal_root` is deliberately EXCLUDED from this
  deeper-strip set (2026-07-15 fix) — a device folder under `Video\Personal`
  is `date-drain`'s job, not flatten's; stripping it in place there was itself
  a live defect (it raced C45 and dropped device files into a flat, undated
  pile). `date-drain` also drains personal VIDEO residue now
  (C45): a Video-bucket file under `layout.personal_root` sitting in a
  provenance/non-year folder lands at `personal_root\<Year>\<filename>`. Year
  precedence (fixed 2026-07-15, live-data defect): a STRONGLY-structured NAME
  date (`imgclass.structured_name_year` — WhatsApp `VID-YYYYMMDD-WA####`/
  `IMG-YYYYMMDD-WA####`, or a leading 14-digit device stamp) is checked FIRST
  and wins over the video's own `mvhd` atom (`vidmeta.creation_year`) — a
  WhatsApp re-encode writes a bogus constant mvhd date, but the filename the
  device wrote is trustworthy; the mvhd date is checked next, then a looser
  name-embedded epoch-ms date (`imgclass.name_year`) — never mtime (C19),
  never a guess. A video with NO date signal anywhere drains to the
  `personal_root\Undated\<filename>` holding shelf (`route:personal:undated`)
  instead of staying stuck in its device folder or flat-piling — it drops the
  device-name provenance without inventing a false date. **C46 collision
  disambiguation:** `reorganize`/`date-drain` accept a
  builder-level `disambiguate` flag (default `False`, preserving every
  standalone `mlo plan` caller's skip-and-report contract); `mlo pilot` opts
  the media drains in. On a DEMONSTRATED content-distinct destination
  collision (byte-identical colliders are always a dedup decision, C21/D12,
  and never reach this path), every surviving member is tagged
  `<stem> [<disc>]<ext>`, `<disc>` being the source's own immediate
  provenance/parent path segment — intrinsic and deterministic, computed at
  PLAN TIME, NEVER a positional `(1)/(2)/(3)` counter (the L1 disaster). A
  shared parent segment falls back to the first 6 hex chars of the file's
  quick_hash. Disambiguated destinations are re-verified for uniqueness; a
  residual clash skips-and-reports (L1/L17 hold throughout).
  The `Ebooks` bucket routes through its own rule family (P17/C43): `route:book:already-placed`
  / `route:book:reshelve` (an Unsorted book whose hints now resolve an author) for files
  already under `Books\`; `route:book:author-series` / `route:book:author` when an author
  is derivable (hint or filename parse); `route:book:unsorted` (honest `Books\Unsorted`,
  never a guess) otherwise — the same evidence precedence as `route:container:member`
  above: identity beats no identity, and no identity means stay honestly unshelved.
- `review` — the judgment block: review-set path (every item carries ALL signals),
  per-queue counts, critic-hinted count, `unsure_relpaths` (the honest human queue),
  `hints_path` (the merged hints Pass 2 reuses verbatim — no model calls in Pass 2),
  critic answers/dissent sidecar paths.
- `llm` — chain used, items sent, hinted/unsure/capped counts (`capped` = review items
  beyond `--critic-limit`, queued for the human, never silently dropped).
- `staging_preview` — per staging root: files/bytes that would be staged by the dedup
  sections. Disposal stays human — `mlo` still has no delete.
- `index` — file count + `journal_pos` at analysis time (the executor reports drift
  against it).

## Approvals (`mlo.approvals/1`) — the Pass-2 gate

The human's review decisions, authored by the web UI (or by hand), consumed by
`mlo pilot --execute --approvals <file>`:

```json
{
 "schema": "mlo.approvals/1",
 "proposal_sha256": "<the reviewed proposal's seal — MUST match>",
 "decisions": {
  "organize:G_phone": "approve",
  "reorganize:library": {"default": "approve",
    "clusters": {"reorganize|Video\Movies\Telugu|route:movie": "reject"}},
  "dedup-library:library": "reject"
 },
 "converge": true
}
```

- Unlisted sections default to **reject** — explicit approval only.
- `proposal_sha256` binds the approvals to the exact reviewed artifact; a mismatch
  refuses with exit 4 (approve-X-execute-X, ledger C25). Partial approvals are resolved
  against clusters RE-DERIVED from the sealed plan and hash-verified — never against
  the proposal's listing alone.
- A partial approval executes as a NEW sealed plan written from the approved row
  subset (the residual-plan mechanism); op_ids are unchanged, so journal idempotency
  holds, and the original plan remains on disk as the audit trail.
- `converge` enables bounded convergence (default max 3 cycles) for the idempotent
  library builders; rejections are sticky across re-plans. Pass 2 makes NO model
  calls — hints are Pass 1's, verbatim.

## Exported CSV views (`.mlo/runs/<run_id>/*.csv`)

The views the engine writes today: `applied.csv` (every `apply`), `ops.csv` /
`files.csv` / `source-<name>.csv` (`mlo export`), and `classify-<source>.csv` /
`triage-<source>.csv` (`mlo agent`). All are **views, not state**:

- **The engine never reads a CSV as input.** State lives in SQLite; the CSVs exist for
  humans, spreadsheets, and grep. This one rule is what makes a stale CSV *inert*
  instead of dangerous.
- Line 1 is a comment row carrying JSON provenance, so a file copied out of context
  still identifies itself:

  ```
  # {"config_hash": "9c41f0e2a77b...", "journal_pos": 54266, "run": "20260706-024109-a1b2c3", "schema": "mlo.csv/1"}
  ```

- Filenames are sanitized (alphanumerics, `-`, `_`) — a source name can never steer a
  view outside the run directory.
- Encoding is UTF-8 with `surrogatepass` — Windows filenames containing lone surrogates
  round-trip losslessly. Paths shown are the display form; the authoritative bytes live
  in the database.
- Every row records **resolved** state (the destination something actually landed at),
  never intended state.

## Exit codes (stable API)

| Code | Meaning | Typical next step |
|---|---|---|
| 0 | Clean success | proceed |
| 1 | Unexpected error (bug, I/O failure) | read the log; file an issue |
| 2 | Refusal by validation: unknown config key, unreachable enabled root, staging inside the library, a plan that would touch protected paths, a forwarded global flag in `agent run --act` | fix `mlo.toml` / the source |
| 3 | Completed **with drift or residuals** (a residual plan was emitted) — also: `verify` found blocking findings, or `agent eval` counted dangerous errors | apply the residual plan / resolve the finding |
| 4 | Stale input refused (artifact older than a mutation in its scope, or `building` from an interrupted scan) | run the printed refresh command |
| 5 | Coverage threshold blocked plan build (unmatched fraction too high) | the error lists the top unmatched tokens — extend `[taxonomy.buckets]` or raise the threshold deliberately |

Codes 2–5 are *refusals by design*, not failures: each one is a defect class from the
[ledger](defect-ledger.md) being stopped at the door. Scripts and agents may rely on
these numbers; changing any of them is a breaking (major-version) change.
