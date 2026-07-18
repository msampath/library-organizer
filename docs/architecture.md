# Architecture

`mlo` is a **plan/apply pipeline over a single SQLite operational store, with exactly one
module allowed to touch the filesystem.** Every command is a pure function from
(config + store + disk) to (artifacts + reports); every mutation flows through a small
safety kernel that enforces the invariants. Dry-run and execute are the same code path —
the only difference is a flag the kernel holds — so a rehearsal cannot diverge in logic
from the real run.

Every design decision below traces to an entry in [defect-ledger.md](defect-ledger.md)
(cited as `L#`). If you are changing this file or the code it describes, read the cited
entries first.

## 1. The trust model

1. **Code enforces safety.** The kernel (`safeops.py`) is the only door to the
   filesystem; its API contains no delete and no overwrite. An AST test fails CI if any
   other module gains filesystem mutation (L0, L18).
2. **Models supply judgment** inside bounded, schema-validated tasks with abstention and
   escalation ([agent-design.md](agent-design.md)).
3. **Humans gate irreversibility.** Staging is same-drive and reversible. Disposal of
   staged content (P21/C2, L18 amended) goes through the OS's own recycle bin / trash —
   never mlo's own delete — and needs a typed row-count confirmation to execute; every
   other module's ban on `rmtree`/`remove`/`unlink` is unchanged.

## 2. The store (`store.py`)

One SQLite database — `.mlo/state.db`, WAL mode, plain `sqlite3`, no ORM — is the sole
authoritative store. **CSV and JSON files are exported views; no engine code path reads
one back** (L7).

Core tables (abridged; full DDL in `store.py`):

```sql
runs(run_id TEXT PK, command, args_json, config_hash, config_json, code_version,
     started_at, finished_at,
     status CHECK(status IN ('running','completed','completed_with_residuals',
                             'failed','interrupted')));

ops(  -- append-only mutation journal; rowid IS the journal position
  op_id TEXT UNIQUE,            -- content-addressed (see §4)
  run_id, plan_id,              -- plan_id is refreshed when a failed op retries
  kind CHECK(kind IN ('stage_move','copy_in','move_within','rmdir_empty','dispose')),
  src BLOB, dst BLOB,           -- utf-8/surrogatepass bytes (canonical)  (L10)
  src_display TEXT, dst_display TEXT,   -- lossy, for humans
  pre_size INTEGER, pre_quick_hash TEXT,
  state CHECK(state IN ('pending','done','skipped_done','skipped_drift',
                        'skipped_protected','failed')),
  detail TEXT, committed_at TEXT);

files(  -- fingerprint index of the library root
  file_id INTEGER PK, relpath BLOB, relpath_display TEXT,
  size INTEGER, quick_hash TEXT, full_hash TEXT,   -- reserved; v0.1 never fills it
  mtime_ns INTEGER, scan_id TEXT);
CREATE INDEX idx_files_fp ON files(size, quick_hash);

source_files(...same shape..., source_name, verdict, verdict_rule);

artifacts(  -- freshness registry (L7)
  artifact_id TEXT PK,          -- 'index:library', 'scan:E', 'verdicts:E', 'plan:<hash>'
  kind, scope_json, built_at,
  journal_pos INTEGER,          -- max ops.rowid at build completion
  config_hash, run_id,
  status CHECK(status IN ('fresh','stale','building','failed','executed')));
```

Notes:
- **Paths are BLOBs.** Windows filenames legally contain lone surrogates; Python's
  `sqlite3` raises on them in TEXT. Canonical storage is
  `path.encode('utf-8','surrogatepass')`; `*_display` is lossy and for grep/joins (L10).
- The DB must not live under any scanned or mutated root — `mlo check` refuses that
  configuration.
- `VACUUM INTO` snapshot at every run start; per-run CSV exports mean history is always
  reconstructible. Worst-case store loss costs a rescan, never user data (the kernel
  can't delete any).

## 3. The safety kernel (`safeops.py`)

```python
class PathPolicy:
    # from config: protected substrings (case-insensitive, matched against the fully
    # resolved \\?\ path), blocked drives, per-drive staging roots, library root.
    # drive_of() is injectable so same-drive rules are testable on tmp dirs.
    def check(self, path) -> Allowed | Blocked(reason)

class SafeOps:
    def __init__(self, policy, store, run_id, execute: bool, plan_id=None,
                 disposer=None): ...
    def stage_move(self, src, dst, pre) -> OpResult   # same-drive enforced; dst under that drive's staging root
    def copy_in(self, src, dst, pre) -> OpResult      # copy -> RE-HASH DST -> journal   (L15)
    def move_within(self, src, dst, pre) -> OpResult  # library-internal move
    def rmdir_empty(self, path) -> OpResult           # os.rmdir ONLY (L18)
    def dispose(self, path, pre) -> OpResult          # P21/C2: OS recycle bin/trash, staging-only (L18 amended)
```

Those five kinds are the entire mutation vocabulary — there is no mkdir op;
parent directories are created implicitly inside the kernel as part of a move
or copy. `dispose` journals `src == dst` (single-path, like `rmdir_empty`) and is
refused (`ValueError`, a plan bug) unless `src` is under a configured staging root.
The actual OS call is injectable (`disposer=`, default the real Windows/POSIX
dispatcher) so tests exercise the kernel's placement/drift/journal logic without
touching a real Recycle Bin or trash directory.

- **There is no delete, unlink, rmtree, or overwrite in the entire codebase.**
  `rmdir_empty` is the only removal via `os.rmdir` and is physically incapable of
  removing content (L18). `dispose` (P21/C2) hands a STAGING-ONLY file to the OS's own
  recycle bin / trash — not a delete primitive either; the file is recoverable through
  the OS's own UI, and `rmtree`/`remove`/`unlink` remain banned everywhere, including
  inside `dispose`. "The engine never deletes user files" is an API-surface property,
  not a policy.
- Every method: normalize to `\\?\` → **policy check on BOTH src and dst** (L12) →
  dst-must-not-exist → journal intent → act → journal done **with the resolved
  destination** (L16). Policy violations and drift return statuses
  (`skipped_protected`, `skipped_drift`); they never raise mid-plan.
- `execute=False` flows the identical path minus the syscall and the journal write —
  dry-run and execute cannot diverge in logic (L9, L17).
- **Enforcement is structural, three layers:** (1) only `safeops.py` imports mutation
  primitives; (2) `tests/test_architecture.py` AST-walks every other module and fails on
  `shutil.*` mutators, `os.rename/replace/remove/unlink/rmdir/makedirs`,
  `Path.rename/unlink/rmdir/mkdir/write_*`, `open(..., 'w'/'a'/'x')` outside the
  whitelist (`safeops.py`; `store.py` for the DB file; `report.py` for the run
  directory — each asserted by path discipline in the same test); (3) runtime policy
  checks at the lowest layer. `dispose`'s Windows Recycle Bin call (ctypes
  `SHFileOperationW`, P21/C2 — not AST-detectable as a mutator by name) gets its own
  dedicated check, `test_shfileoperation_only_in_safeops`: that literal string may
  appear nowhere outside `safeops.py`.

## 4. Idempotency (L1)

`op_id = sha256(canonical_json({kind, src_bytes, dst_bytes, pre_size, pre_quick_hash}))`
computed at **plan time** and fixed in the plan row.

Execute flow per row:
1. Journal lookup by `op_id` → **a proven-`done` op is a no-op** (`skipped_done`). A
   re-run of a completed plan prints "N already done, 0 performed" and exits 0. A prior
   drift/protected/failed skip is *not* terminal — it is re-derived from live disk every
   run, so a residual plan can actually retry once the cause is fixed (defect C1).
2. Re-verify preconditions on disk (exists / size / quick-hash) → drift →
   `skipped_drift`, continue (L9).
3. Journal intent (`pending`) and **commit it** — then act — then mark `done` in the
   same transaction as the library-index effect, commit (defect C2). A retry refreshes
   the op's `plan_id` so attribution follows the run that actually acted.

**Crash recovery:** on next run, durable `pending` rows are reconciled against disk: dst
present with expected hash and src gone → `done` (with the index effect the kernel would
have applied — defect C2); src intact and dst absent → retry; both present → `failed`
with detail, surfaced, never auto-resolved. A crash can cause neither re-execution nor a
silently forgotten move.

**No execute-time naming.** dst is fixed at plan time; occupied dst = drift. The
`(1)/(2)/(3)` collision-resolver disaster (L1) has no code path. Reinforced at plan
level: **plan validation rejects duplicate destinations** (L17), so intra-plan collisions
are impossible rather than resolved.

Durability is per-op (one commit for the intent, one for the outcome) — the safe
default for the data at stake. Chunked commit batching is a documented future
optimization ([roadmap.md](roadmap.md)), acceptable only because the reconciler makes a
chunk window crash-safe.

## 5. Plans and apply (L2, L5, L9, L13, L17)

Plans are **hash-stamped JSONL artifacts** (schema in [formats.md](formats.md)): header
carries config hash + input-artifact freshness stamps; body rows carry op_id, kind,
src/dst, preconditions, and rule provenance (`reason.rule`); footer carries **two**
hashes: `plan_id` — *semantic* identity over (kind, source, config, inputs, rows),
deliberately excluding the created timestamp so an identical rebuild IS the same plan
(C3/C11-era fix: a timestamp in the id made executed-preservation timing-dependent) —
and `content_sha256`, byte-level file integrity over exactly what's on disk.

The three core plan kinds: `organize` (source UNIQUE files → content-derived
Jellyfin destinations, §7, with provenance-flat fallback when identity isn't
derivable), `dedup` (ORGANIZED+JUNK → same-drive staging), and `reorganize`
(library-internal `move_within` restructuring — see §7's repair contract).
The library-side movers extend the same shape — `dedup-library`,
`stage-library`, `prune-empty`, `date-drain`, `flatten-provenance`,
`containers`, `relocate`, `bad-archives` — one builder each in plan.py; the
kind→op mapping lives in docs/formats.md.

- `mlo plan …` refuses stale inputs (exit 4, prints the refresh command), refuses rows
  that touch protected paths (exit 2 — defect C5), and rejects duplicate destinations
  (exit 2 — L17). Staleness tracks **engine** mutations; an externally mutated source is
  caught by execute-time re-verification, not at plan build (the honest scope of L13).
  **Stage plans for a source require its organize plan executed or explicitly waived** —
  copy-before-stage is an ordering the builder enforces, not a convention (L13).
- `mlo apply plan.jsonl` (no flag) is the rehearsal: verifies plan integrity, re-checks
  every row's preconditions against live disk (missing sources are *reported*, not
  previewed as work — L17), reports would-do + current drift.
- `mlo apply plan.jsonl --execute` runs §4 per row, then the **automatic post-condition
  audit**: dst present + hashed, src absent for moves. Any non-done outcome — drift,
  failure, protected skip, unmet post-condition — joins the residuals ⇒ run status
  `completed_with_residuals`, exit 3, and an auto-emitted residual plan, itself
  registered as an artifact (L5, C1, C5). A fully-executed plan re-applies as an
  all-no-op, and rebuilding it cannot revert its `executed` status (C3). There is no
  `--force`.

## 6. Freshness (L7)

Every artifact records the journal position at build completion plus its scope and
config hash. Staleness = any later `done` op that **mutated** a path in the scope
(a `copy_in` only reads its source, so organizing a source does not invalidate that
source's own dedup plan), or a config change. Mutating ops flip intersecting artifacts
stale **and update the `files` index in the same transaction** — the library index
cannot go stale from engine actions; sentinel rituals do not exist. Scans register
their artifact as `building` up front and flip it `fresh` only on completion, so an
interrupted scan can never present as truth (C6). Consumers refuse stale inputs
(exit 4) and print the exact refresh command to run — there is no auto-refresh; the
remedy is named, the human runs it.

External edits are invisible to any journal — that is L14's lesson, and the honest
answer is layered: execute-time re-verification is the hard backstop; `mlo verify
library` stat-diffs the library against the index (add `--deep` to re-fingerprint every
file and catch same-size/same-mtime edits); journaled staging means unexplained content
in a staging root is itself a verify finding.

## 7. Classification, the router, and coverage (L4, L6)

Classifiers (junk rules, taxonomy buckets) are pure total functions:
`classify(item) -> Match(label, rule_id) | UNMATCHED`.

- No implicit 'Other': a catch-all exists only as an explicit rule with its own
  `rule_id`; absence of a match is UNMATCHED — counted, reported, routed to REVIEW.
- Plan build computes coverage; unmatched share above `[classify] max_unmatched_pct`
  **fails the build** (exit 5), and the refusal itself carries the top unmatched tokens
  ranked by frequency: a missing majority keyword names itself in the error (L4).
- All rule tables live in `mlo.toml` (L6) — a CI test bans protected-path literals in
  code. Every plan row carries rule provenance.

**The hierarchical router (v0.2).** `taxonomy.route(cfg, relpath, hints)` derives
content-based, Jellyfin-compatible destinations — `Movies/<Language>/Title (Year)/`,
`TV_Shows/<Language>/<Series>/Season NN/`, `Music/<Language>/<album>/`,
`Photos/<year>/` — replacing v0.1's provenance-flat placement ("organization by
definition includes meaningful groupings"). Its contract, each clause pinned by a
router test:

- **Pure and hint-driven**: EXIF years and agent classifications arrive as `Hints`
  arguments; the router does no I/O. Media-name parsing lives in `naming.py`, the L3
  answer made strict: a year is ONLY a parenthesized 1900–2035 (the last one in the
  stem), episodes are `SxxEyy` (primary) or `NxNN` (word-bounded), titles get
  separator normalization only — all total functions, property-tested.
- **Idempotent / already-placed first**: a file inside a structurally-valid home keeps
  it — a movie folder whose `(Year)` matches the file, any `Season NN` home under the
  TV root, anything under `Music/<Language>/`, photos already in a year folder absent
  contradicting EXIF. The router improves the unorganized; it **never second-guesses
  valid existing structure**, including hand-named folders (`'Friends (1994)'` stays,
  C13). This is what makes `reorganize` converge to zero rows.
- **Positional language precedence**: a directory segment that IS a configured language
  beats language tokens in directory names, which beat tokens in the filename — so a
  `.English.Subs` tag can never move a file out of its `Tamil/` folder (C11) — then
  agent hints, then the *explicit* `default_language` (with rule provenance; not an
  implicit bucket).
- **Never-guess**: no derivable identity → `None`. Organize falls back to
  `<Bucket>/<source>/<relpath>`; reorganize leaves the file where it is and exports the
  unrouted media list as a JSON sidecar for `mlo agent classify --media`.
- **Content-sniff false-carves** (`reorganize --sniff`): when an extension yields **no**
  taxonomy bucket, an optional magic-byte sniff (`sniff.py`) may identify the file as
  video/audio/image; the router then reclassifies it into `<MediaType>/Unclassified/` (a
  holding pen), keeping its parent grouping. Idempotent and conservative: a file already
  under a media top segment is never reshuffled, so a moved carve converges. Content is
  consulted only in the no-bucket branch — it can never override a configured extension —
  and the sniffed kind rides the same `Hints` seam as an EXIF year.

**The repair contract** (`plan reorganize`): only paths under the `--under` prefixes are
even examined; route-equals-current yields no row; destination collisions and
already-occupied index destinations stay put (never renamed around — L1/L17); and the
whole thing is an ordinary plan — rehearsed, gated, journaled, convergent.

## 8. Verdicts

Per source file, against the library fingerprint index — the fingerprint is
`(size, SHA-256 of first 128 KB + last 128 KB)`: **ORGANIZED** (fingerprint-identical to
indexed content) · **JUNK** (explicit junk rule) · **UNIQUE** (not in index) ·
**REVIEW** (matched no rule — see L4). Verdict artifacts carry scan stamps and refuse to
feed plans when stale (L13). v0.1 does **not** escalate to full-file hashes for verdicts
(a same-size file differing only in the middle would verdict ORGANIZED — see §13 and
[roadmap.md](roadmap.md)); `verify --deep` and reversible, human-gated staging bound the
consequence.

## 9. Config (`mlo.toml`) (L6, L8)

Library root · `[[sources]]` with explicit `enabled = false` (dead infrastructure is
data, not comments — L8) · per-drive staging roots · protected substrings + drives ·
junk rules · taxonomy buckets · coverage threshold · `[layout]` + `[layout.languages]`
(the Jellyfin-default segment roots, `default_language`, and language-token tables the
router consumes — see §7) · `[llm]` chain (see [agent-design.md](agent-design.md)).
Startup validation on every command: unknown keys → exit 2; unreachable enabled root →
exit 2 naming the two legal remedies.

## 9b. The 2-pass surface (`pilot.py`, `web.py`)

`pilot.py` composes the gated primitives (the `sweep.py` precedent — zero new
filesystem power) into the product's two passes. Pass 1 (`analyze`) runs scan ->
verdicts -> every applicable builder -> full-signal review-set -> critics -> hinted
re-plan -> per-section rehearsal, and seals the result as `mlo.proposal/1`; the ops
journal is provably untouched. Pass 2 (`execute`) binds approvals to the reviewed
proposal by hash (ledger C25), executes approved sections in dependency order with
bounded convergence (no model calls; Pass-1 hints verbatim; sticky rejections), and
ends in the verify tail. `web.py` (`mlo serve`) is a localhost-only consumer of those
sealed artifacts: it renders and gates, and every mutation still flows through the
same plan/apply pipeline.

## 10. CLI and exit codes

`mlo init | check | status | doctor | scan | verdicts |
sweep [source ...] [--confirm-mb N] [--execute] |
pilot [--execute --proposal F --approve-all] [--live-search] | serve [--port N] |
plan <kind> (see §5 for the kinds) |
apply [--execute] [--confirm-dispose N] | undo <run_id> |
dispose [--staging KEY] | identify [--source S | --review-set F] |
verify [--deep] | snapshot | export` + `mlo agent classify
[--media --paths F --out N] | critics | triage | improve | run | eval
[--chain C]`, all with a global `-v/--verbose`. `pilot`/`serve` are the
2-pass front door (docs/runbook.md).

`sweep` is the productized source-drive consolidation: for each configured source
it scans → verdicts → stages the already-in-library originals out (`dedup` with a
`--confirm-mb` twin re-confirm), while **holding** any source that still has UNIQUE
(only-copy) files so a human preserves them first (`mlo plan organize`) rather than
a sweep laundering an unvetted only-copy into the curated library. It composes the
gated primitives and adds no filesystem power — the orchestration that used to be an
ad-hoc operator script is now one auditable, journaled, resumable command
([sweep.py](../src/mlo/sweep.py)).

Exit codes are API: **0** ok · **1** unexpected error · **2** config/validation/refused
plan · **3** completed with drift/residuals, blocking verify findings, or eval dangerous
errors · **4** stale input refused · **5** coverage threshold blocked. `apply` and
`agent eval` write a `summary.json` whose `suggested_next` holds exact CLI strings —
that pair (codes + summary) is the agent interface ([formats.md](formats.md)).

## 11. Testing strategy

- **`test_architecture.py` is CI law** — the AST ban that makes §3 structural — and
  **`test_defect_ledger.py`** makes the ledger's named-test citations enforceable.
- Property-based (hypothesis): surrogate round-trips (L10), classifier totality (L4),
  plan JSONL round-trip + plan_id stability (L3's class: no fragile string munging).
- **Crash-injection idempotency**: raise mid-apply, rerun, assert zero duplicates and
  eventual completion (L1); plus reproduction tests for every review-found defect
  (`tests/test_regressions.py`, C1–C10, including the 2nd-order fix-interaction seams) and
  the security findings (`tests/test_security.py`).
- Windows matrix (skipif elsewhere): >260-char trees, lone-surrogate names, reserved
  device names. Fake `drive_of()` injection makes same-drive rules testable on tmp dirs.
- CI: windows-latest + ubuntu-latest (portability kept honest cheaply).
- **Dogfood** (measured 2026-07-06): `mlo scan library` + `mlo verify library` against a
  real **388,609-file / ~1.9 TB** library — structurally read-only (those commands never
  construct an executing kernel). Result: the scanner indexed all 388,609 (0 unreadable,
  0 errors) — an *exact* independent reproduction of the count a completely separate
  toolchain produced for that library — `mlo status` showed **journal position 0** (proof
  nothing was mutated), and `verify library` returned **0 findings** across all six
  categories (scanner/verifier internally consistent at scale). C1–C10 regression tests
  (`tests/test_regressions.py`) plus the security findings (`tests/test_security.py`) pin
  every review-found defect; the crash-injection idempotency test guards L1.

## 12. Module map

```
src/mlo/
  safeops.py     KERNEL: PathPolicy + SafeOps (§3); dispose (P21/C2/C68) is the
                 ONE ctypes call in the codebase (Windows Recycle Bin), enforced
                 kernel-only by test_shfileoperation_only_in_safeops
  store.py       SQLite: schema, Journal, Index, Artifacts, Runs (§2).
                 SCHEMA_VERSION 2 (P21/C68: ops.kind widens for 'dispose');
                 _migrate_v1_to_v2 upgrades an existing v1 workspace in place
  winpath.py     \\?\ canonicalization, surrogatepass codecs, drive_of()
  staging.py     pure staging-root resolution (P21/C53): drive-letter/UNC exact
                 match + POSIX absolute-path-prefix longest-match; same_volume()
  trash.py       pure disposal computation (P21/C2/C68): POSIX XDG same-device
                 trash-dir resolution, both-dir unique naming, percent-encoded
                 .trashinfo payloads; the staging-only guard is safeops
                 _placement_error; the real OS call lives in safeops.py, not here
  config.py      TOML load, unknown-key rejection, reachability + placement validation (§9)
  fingerprint.py pure: quick = (size, sha256 of head+tail 128K); full() is used by
                 build_dedup_library's confirmation pass and confirm_duplicate()
  scan.py        walkers, batched store writes; library scan has a stat fast-path resume
  verdict.py     file vs index -> ORGANIZED/JUNK/UNIQUE/REVIEW (§8)
  naming.py      strict media-name parsers: Title (Year), SxxEyy (the L3 answer)
  containers.py  pure semantic-container matcher (C33): subtrees that move as
                 UNITS (phone backups, drive images); canonical home of
                 MEDIA_LABELS + PROVENANCE_SEG (taxonomy re-exports)
  taxonomy.py    bucket rules + the hierarchical router route() (§7); coverage
  exif.py        stdlib-only, total DateTimeOriginal year reader (JPEG/TIFF)
  sniff.py       stdlib-only, total magic-byte content-kind sniffer (false-carves)
  plan.py        all plan builders -> JSONL plans; all build gates (§5); every
                 builder funnels through _register_plan, which also runs the
                 capacity.py free-space preflight (P21/C64). build_dispose
                 (P21/C2/C68): journal-explained staged files -> a dispose plan
  undo.py        P21/C63: mlo undo — a run's DONE move_within ops -> a reverse
                 plan (dst->src, LIFO), through the normal plan path; copy_in/
                 stage_move/dispose are counted in notes (no inverse op kinds)
  capacity.py    pure free-space preflight (P21/C64): bytes needed per dest
                 volume (copy_in rows only) vs bytes free (no shutil — banned
                 repo-wide; GetDiskFreeSpaceExW/os.statvfs directly)
  doctor.py      P21/C66: mlo doctor — version, config, EVERY root's
                 existence+writability (closes the staging-reachability gap
                 config.validate never checked), store/journal health, LLM
                 chain preflight, last run
  apply.py       idempotent execute loop, crash reconciler, audit tail (§4, §5);
                 verbose= surfaces crash-reconciled op_ids (P21/C65); a
                 'dispose' plan needs confirm_dispose=<exact row count> to
                 execute (DisposeNotConfirmed otherwise, P21/C2/C68)
  verify.py      library-vs-index diff (--deep re-fingerprints), staging findings
  report.py      plans, proposal seal, summary.json, JSON sidecars, CSV exports
  pilot.py       the 2-pass DAG: analyze -> sealed proposal -> gated execute + convergence
  web.py         mlo serve — localhost-only 2-pass UI over pilot (token + Host gated)
  hints.py       per-file identity hints + EXIF/sniff augmentation seam;
                 augmenters take verbose= for per-file stderr chatter (P21/C65)
  sweep.py       productized source-drive consolidation (scan -> verdict -> stage)
  snapshot.py    per-folder problem inventory (the eval loop's input)
  selfimprove.py the self-improving eval loop (measure -> propose -> gate)
  interview.py   onboarding config interview (init --interview)
  distill.py     evidence distillation for the agent seam
  seam.py        engine<->agent review-set builder (all-signals, §3.3)
  audioclass.py  deterministic 'not all audio is music' pre-classifier
  imgclass.py    deterministic 'not all images are photos' pre-classifier
  pathmeta.py    full-path metadata extractor (holding-pen awareness)
  docmeta.py     OOXML document-property reader; ole.py = legacy CFB fallback
  bookmeta.py    ebook identity (P17/C43): epub OPF / mobi EXTH readers, pure
                 filename parse, "Last, First" shelf-author normalizer
  vidmeta.py     stdlib-only, total MP4/MOV creation-year reader (P18/C45):
                 the mvhd atom, read-only, capped at the first 1 MiB
  fuzzy.py       title matching: exact -> token (P21/C57, thefuzz-or-stdlib) ->
                 Damerau-Levenshtein -> guarded Soundex; abstains on ties
  hashdrift.py   recompute fingerprints after a (future) metadata write
  identify.py    P21/C59: mlo identify — batched critic-panel loop over a
                 review-set -> ONE schema-validated merged hints file
  dotenv.py      P21/C61: minimal read-only .env loader (no third-party dep);
                 env vars always take precedence over the file
  agent/         llm chain (+ preflight, P21/C60), protocol, tasks (incl.
                 classify --media), critics, evals (+ eval_critics, P21/C62)
  enrich/        opt-in connectors: TMDb, ID3, subs (OpenSubtitles search, P21/C58),
                 web search, searxng (P21/C55 --live-search transport),
                 mediafacts (P21/C56: id3/tmdb -> review-set), evidence
  cli.py         argparse; hints/EXIF assembly at the edge; results -> exit codes
```

## 13. Known limits (stated, not hidden)

- Freshness stamps track **engine** mutations; simultaneous external writers during
  `apply --execute` are out of scope (single-mutator assumption, documented).
- Fingerprints default to (size, head, tail) — verdicts and `copy_in`'s read-back use
  the quick fingerprint. STAGING-OUT decisions escalate: `fingerprint.confirm_duplicate`
  (P21/C52) full-hashes both sides above 256 KiB, and `build_dispose` (C68) additionally
  re-verifies content against the journaled fingerprint before planning disposal. The
  quick-only remainder (verdict nomination) is acceptable because staging is reversible
  and disposal is confirmation-gated; `verify --deep` re-fingerprints everything.
- POSIX `rename` overwrites silently where Windows fails; the kernel exists-checks and
  refuses cross-device moves up front (no claim files — the engine could never delete a
  leaked one). Windows, where `os.rename` is natively no-clobber, is the primary target.
- EXIF year reading covers JPEG and TIFF (`DateTimeOriginal`, fallback `DateTime`);
  TIFF-based RAW (`.dng/.kdc/.cr2/.nef/.arw/…`, in the starter `Photos` bucket) reads
  through the same path since the parser keys on magic bytes, not extension. PNG/HEIC
  carry no readable date without a dependency and return None — those photos
  route to `Photos/Unsorted/`, never a guessed year.
- The router derives a TV series name from the *filename* (the part before `SxxEyy`);
  a bare `S01E02.mkv` inside an unnamed folder lands under `Unknown Series`. Existing
  series folders are respected via the already-placed rule, not re-derived.
