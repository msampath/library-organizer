# Changelog

All notable changes to `mlo` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Every fix below traces to an
entry in [docs/defect-ledger.md](docs/defect-ledger.md).

## [Unreleased]

### Fixed (P21 A–C super-review — 23-agent adversarial pass, ledger C69)
- **Windows dispose was dead on arrival**: `SHFileOperationW` rejects `\\?\`-prefixed
  paths (0x7C, empirically reproduced) — the kernel now hands it the plain path; dispose
  is consequently MAX_PATH-bound on Windows (a longer path journals `failed`, file
  untouched).
- **`dispose-residual` plans bypassed `--confirm-dispose`** — the C68 gate now covers
  them, and the suggested next command carries the confirmation.
- **`mlo undo` of a staging run crashed apply** — `stage_move` reversal fits no kernel op
  kind; undo is now honestly scoped to `move_within`, reports the rest, uses the
  journal's lossless paths (L10), and skips replaced-content targets.
- **`mlo serve` never loaded `.mlo/.env`** — web critic chains silently lost the CLI's keys.
- **`mlo doctor` refused to run against broken setups** — the one command that should
  report `[MISSING]` now diagnoses instead of exiting 2.
- **Store migration hardening**: transactional v1→v2 rebuild (a crash no longer bricks
  the store), explicit rowid copy (journal positions preserved), and pre-versioning
  workspaces (user_version 0 with the old schema) now migrate instead of being stamped.
- **Capacity preflight aggregates per volume**, not per destination folder — a combined
  shortfall on one disk now warns.
- Plus ~40 smaller confirmed fixes: hint augmenters no longer drop fields on rebuild,
  `identify --prior-hints` skips already-answered items, the 429 retry waits
  (Retry-After honored), date-drain honors curated year subtrees at any depth,
  comics/ebooks roots join route()'s cross-type protection, interview TOML escaping,
  POSIX trash claim ordering + percent-encoding, dangling-symlink no-overwrite (lexists),
  enrich malformed-body guards, config refusals for empty protected substrings and
  scheme-less searxng URLs, honest pilot drift/escalation accounting, and a docs truth
  pass (README/runbook/formats/roadmap/architecture now describe the shipped C68
  constitution). Full detail: ledger C69.

### Added (P21 Phase C — undo, preflight, diagnostics, doctor)
- **`mlo undo <run_id>` (C63)** — a reverse plan for a run's DONE `move_within`/`stage_move`
  ops (dst→src, LIFO), built and applied through the normal plan/apply path so every existing
  gate still applies. `copy_in` ops (no delete primitive exists) and drifted targets are
  reported in the plan's notes, never silently dropped.
- **Free-space preflight (C64)** — new `capacity.py`: every plan now carries a WARNING note
  when a destination volume looks short on space for its `copy_in` rows, wired once at the
  shared `plan._register_plan` choke point (all builders + `mlo undo`) and inherited
  automatically by `mlo pilot`'s proposal sections.
- **`-v`/`--verbose` (C65)** — per-file diagnostic chatter to stderr for the hint augmenters,
  the full unreadable-file list on `mlo scan` (previously a silent top-10), and crash-reconciled
  op_ids on `apply --execute`. No log files — the SQLite journal remains the record of what the
  system did. Fixed a real bug found while wiring this: `reconcile_pending`'s verified-copy_in
  branch never populated its `verbose` list (the count was right, the list wasn't).
- **`mlo doctor` (C66)** — one command a support flow can ask for: version, config, every root's
  (library/sources/staging) existence *and* writability — closing a gap `mlo check` never
  covered, since `config.validate` has never checked staging roots at all — store/journal
  health, LLM chain preflight, and the last run's outcome.
- **Surfaced crash recovery (C67)** — `mlo check`, `mlo status`, and `mlo serve`'s boot now all
  detect leftover pending journal rows and name the remedy, instead of a crash's only visible
  trace being a silent `reconcile_pending` buried inside the next `apply --execute`.

### Added (P21 Phase C2 — recycle-bin dispose, the L18 amendment, owner go-ahead 2026-07-17)
- **`mlo dispose` (C68)** — the first delete-adjacent kernel primitive: a new `dispose` op
  kind in `safeops.py` sends STAGING-ONLY files to the OS's own recycle bin / trash (Windows
  Recycle Bin via `SHFileOperationW`+`FOF_ALLOWUNDO`; POSIX XDG trash via new `trash.py`) —
  never mlo's own delete. `rmtree`/`remove`/`unlink` remain banned everywhere, enforced by a
  dedicated new architecture test. `mlo dispose [--staging KEY]` builds a plan over files the
  journal recognizes as this engine's own staged output; executing needs
  `mlo apply <path> --execute --confirm-dispose <exact row count>` — a typed, scriptable
  confirmation so disposal is never armed by habit. `store.SCHEMA_VERSION` 1→2 (the ops.kind
  CHECK constraint widens); an existing workspace migrates in place on next open.

### Added (P21 Phase B — connectors live, continued: identify, provider chain, evals)
- **`mlo identify` (C59)** — the productized identification loop: slice a review-set into
  batches, run the critic chain, merge into ONE schema-validated hints file, with optional
  incremental resumption from a prior hints file. Closes the hand-JSON friction of the
  out-of-engine workflow the owner previously ran by hand.
- **Bounded retry + LLM chain preflight (C60)** — `ChainClient` retries once on a transient
  HTTP 429/503 before failing over; new `agent/llm.preflight()` probes chain-entry
  reachability, wired into `mlo check` (informational — never fails the command).
- **`.mlo/.env` support (C61)** — a minimal, dependency-free `.env` loader for cloud API keys
  (`MLO_ANTHROPIC_KEY`, `MLO_GEMINI_KEY`, `MLO_TMDB_KEY`, `MLO_OPENSUBTITLES_KEY`), loaded at
  CLI startup; process env vars always take precedence.
- **Critic-panel eval runner (C62)** — `agent/evals.eval_critics` + `evals/critics.json` (21
  items): the first accuracy measurement for the panel that actually resolves media identity,
  wired into `mlo agent eval` automatically when the golden set is present.

### Added (P21 Phase B — connectors live)
- **`--live-search` (C55)** on `mlo pilot` and `mlo agent critics` — the composed
  web-search query is now actually searched against a self-hosted `[enrich] searxng_url`
  (new `enrich/searxng.py` adapter), closing the "ghost query" where a query was composed
  and attached to critic evidence but the internet was never queried.
- **ID3/TMDb evidence producers (C56)** — new `enrich/mediafacts.py` gives the
  previously-dead `id3.read_tags`/`tmdb.search_movie` connectors first-class producers
  (`tags_map`/`movie_candidates`), and `seam.build_review_set` gained `media_tags=`/
  `title_candidates=` params rendered into the critic prompt. NOT yet wired into the
  production pilot/identify call sites (the config gates + root threading are tracked
  follow-up work — ledger C56 records the same deferral); `[enrich]` `tmdb_enabled`/
  `id3_enabled`/`opensubtitles_enabled` are accepted config keys that no code path
  consults yet.
- **Word-order-insensitive title matching (C57)** — `fuzzy.best_match` gained a token tier
  (`thefuzz`'s token_sort/token_set ratios via the new `thefuzz` `enrich` extra, or a
  stdlib exact-reorder fallback) so 'Rings, The Lord of the' matches 'The Lord of the
  Rings' — Damerau-Levenshtein alone never saw reordered titles.
- **OpenSubtitles connector (C58)** — `enrich/subs.py` rebuilt on the OpenSubtitles REST
  API v1 (search only), replacing the dead TheSubDB-era code.
- New `[enrich]` config section (`searxng_url`, `tmdb_enabled`, `id3_enabled`,
  `opensubtitles_enabled`) — endpoints/toggles only; API keys stay env-var-only
  (`MLO_TMDB_KEY`, `MLO_OPENSUBTITLES_KEY`), never accepted in `mlo.toml`.

### Fixed (P21 Phase A — product-readiness program, core integrity)
- **The B1 blocker (C53)**: `[staging]` is no longer Windows-drive-letter-only. New
  `staging.py` resolves UNC shares and POSIX absolute-mount paths as staging roots
  (longest-prefix match), fixing an invalid-TOML/invalid-path bug in the onboarding
  interview's UNC handling and a silently-empty `[staging]` table on POSIX. The disposal
  half of the product (`dedup`, `dedup-library`, `stage-library`, `bad-archives`) is now
  reachable on Linux/macOS and NAS/UNC shares — the CLI e2e disposal tests are un-gated
  from Windows-only.
- **`copy_in` preserves source `mtime` (C51)** — consolidating a file no longer stamps it
  "today"; capture/modified dates survive into the library.
- **Full-hash escalation for staging-out decisions (C52)** — `fingerprint.confirm_duplicate`
  replaces ad-hoc, caller-sized region confirmation with a fixed policy: quick match, then a
  full streaming SHA-256 above 256 KiB — never a same-size/same-ends/different-middle false
  positive, regardless of file size or the `--confirm-mb` value.
- **Bounded `fingerprint.region()` reads (C50)** — hashes in 4 MiB sub-chunks instead of one
  allocation sized to the caller's chunk, removing an OOM risk on a large `--confirm-mb`.
- **Hardlink-aware library dedup (C54)** — `build_dedup_library` no longer stages a hardlink
  to an already-represented copy as a "duplicate" (it would reclaim no space); decisive for
  NAS backup shares (rsnapshot/`rsync --link-dest`/Time-Machine-style snapshots), which
  create hardlink farms that would otherwise flood dedup with false positives.

### Added
- **The 2-pass product** — `mlo pilot` (Pass 1: analyze everything into ONE sealed
  proposal) and `mlo pilot --execute` (Pass 2: approvals-gated execution with bounded
  convergence), plus **`mlo serve`**, the localhost-only guided web UI over the same
  flow (session-token + Host-checked, single-mutator gated).
- **Semantic containers (C33/C39)** — `plan containers`: phone backups, drive images
  and app exports move as units to device-keyed homes (`Backups\Phones\<S5|Nexus6|…>`),
  with merge-by-identity, byte-identical dedup and owner disambiguation (D10-D12);
  once home, the tree is claimed forever (C39).
- **The P13 wave** — any-depth provenance flatten (C34), non-media bucket routes +
  Comics series normalization (C35), sidecar handling (C36, corrected by C38/C40/C41),
  bad-archive detection (`plan bad-archives`, C37), `plan relocate --map` explicit mover.
- **Pass-1 convergence rehearsal (C42/P16)** — `mlo pilot` now seals the projected
  END-STATE of the pure-index movers (containers/reorganize/date-drain/flatten),
  rehearsing the whole Pass-2 convergence chain against an in-memory index copy, so
  the reviewed proposal is what actually executes; Pass-2 records a per-section
  `convergence_delta` for any execute-time divergence. Closes the measured gap where
  convergence executed the bulk of a large run unreviewed.
- **P15 super-review hardening** — store schema version stamp + corrupt-db remedy +
  retry-preserved pre-fingerprints; crash-reconcile rmdir/audit corrections; upfront
  approvals validation and plan_id binding in Pass 2; config type refusals
  ([llm] chain-as-string, wrong-typed sections, non-numeric values); linear-time
  sidecar collection; `[classify.image_patterns]` + devotional/lost audio categories.
- **`mlo sweep`** — productized source-drive consolidation: for each configured source,
  scan → verdict → stage the already-in-library originals out, **holding** any source that
  still has UNIQUE (only-copy) files so a human preserves them first. Rehearsal by default,
  `--execute` to stage, `--confirm-mb N` for the twin re-confirm. Replaces ad-hoc
  scan/verdict/plan/apply operator scripts with one auditable, journaled, resumable command.
- **`plan dedup --confirm-mb N`** and `fingerprint.region()` — re-confirm each ORGANIZED
  file against its library twin at N MiB head+tail before staging it out; a same-size /
  same-ends / different-middle file is kept in place, never swept off its only unique content.
- **Ebooks (P17/C43)** — a new `Ebooks` taxonomy bucket and `Books\` top-level root:
  `bookmeta.py` reads embedded epub OPF / mobi EXTH metadata (identity precedence:
  embedded > filename parse > hinted), shelves authors as "Last, First"
  (particle-aware), and groups series under the author. Unidentified books land
  honestly in `Books\Unsorted`, never guessed. Identity for title-only/ambiguous
  books is judged out-of-engine by Claude Opus subagents against the existing
  `--hints` surface — no new `[llm]` calls. Phase A (format bucket + embedded/filename
  identity); pdf/doc/rtf/txt judgment tier is Phase B, deferred.
- **RAW photo support.** TIFF-based RAW extensions (`.dng .kdc .cr2 .nef .arw .orf .raf
  .rw2`) are in the starter `Photos` bucket and read their EXIF year through the existing
  stdlib parser (it keys on magic bytes, not extension). A year-attested RAW routes to
  `Photos/<year>/`; a yearless one stays put under the C19 evidence rule.
- **Magic-byte content sniffing** (`sniff.py`, stdlib-only, total, read-only) and
  **`plan reorganize --sniff`** — the answer to the false-carve pile: a recovery blob
  written into a `.swf`/`.dat`/`.au` whose extension lies is routed by what it IS. An
  in-scope file with **no** taxonomy bucket is sniffed by its leading bytes; an identified
  video/audio/image carve reclassifies into `<MediaType>/Unclassified/` (a holding pen a
  critic or human then places). Content is consulted only in the no-bucket branch — it
  never overrides a configured extension — and rides the same `Hints` seam as an EXIF year;
  a headerless blob honestly gets no hint and stays put.
- **Agent chain ledger persistence.** `mlo agent eval` writes each call's chain ledger
  (entry, outcome, latency, fallback hops) to `agent-ledger.jsonl` and a `chain_ledger`
  rollup to `summary.json` — closing the agent-design §1 "not yet persisted" gap.
- **Chain-selectable eval.** `mlo agent eval --chain local,claude-haiku-4-5` measures a
  specific configuration; the local slot wakes only when `local` is in the chain, so a
  cloud-only row never touches Ollama.
- **Specialist skills** (offline, stdlib): `fuzzy.py` (disc/part-token stripping →
  Damerau-Levenshtein → guarded Soundex, abstains on ties); `provenance.py` (journal-traced
  origin, honest coverage boundaries, INFORMS only); `fingerprint.confirm_same()` (the
  byte-twin check a critic must pass before calling a file redundant).
- **Finer taxonomy** (§7) via config-driven `[layout.subtypes]`: a critic-assigned
  media_kind (WhatsApp, Anime, Ads, Sports, Audiobooks, System_Sounds, Screenshots,
  Graphics) routes to its sub-root — folder names in config, never in code (L6).
- **The engine↔agents seam**: `seam.py` builds a self-contained review-set (fingerprint +
  provenance + an enumerated candidate-home menu) from the reorganize residue;
  `distill.py` turns recurring critic judgments into a `[classify.name_patterns]` rule the
  engine applies next run with **no model call** (rule-diff emitted for human approval).
- **The classification critic panel** (`agent/critics.py`, `mlo agent critics`): per-language
  Movie/TV critics (transliteration-aware, fuzzy-backed), Music, Photo (real photo vs
  screenshot vs graphic), and an **adversarial tiebreak** for disagreements. Bounded,
  schema-validated, abstaining (UNSURE → Unclassified/human), local-capable.
- **Enrichment connectors** (`mlo.enrich`, opt-in `mlo[enrich]`): TMDb, the SubDB
  content-hash scheme (head/tail MD5; the TheSubDB service itself is defunct, so the
  fetch is historical — an OpenSubtitles connector remains a roadmap row), mutagen ID3,
  batched web search — fetch/parse/render/hash only, offline core preserved;
  `hashdrift.recompute()` keeps dedup honest after a future metadata write.
- **The self-improving eval loop**: `mlo snapshot` (per-folder problem inventory),
  `evals/dogfood` fixtures + `evals/known-failures.jsonl` (regression guard), and
  `mlo agent improve` — score → diagnose → distil a rule → re-score → keep iff more-correct
  AND safe; **dangerous or regression is a hard stop** (exit 3). Dry-run, rules only, never
  the library. `summary.json` carries a full `failure_modes` block.
- **Onboarding interview** `mlo init --interview` — generates a valid, `mlo check`-passing
  config from the parameterized surface (sacrosanct folders, sources, languages, model).

### Changed
- Publish polish: `[project.urls]`, README status badges, this changelog, and a
  `CONTRIBUTING.md` pointing at the working agreement.

## [0.2.0] — 2026-07-06

### Added
- **Jellyfin-default hierarchical router** (`taxonomy.route`): content-derived
  `Movies/<Language>/Title (Year)/`, `TV_Shows/<Language>/<Series>/Season NN/`,
  `Music/<Language>/…`, `Photos/<year>/`, replacing v0.1's provenance-flat placement.
  Strict, property-tested media parsers in `naming.py` (the L3 answer, made total).
- **`plan reorganize`** — in-place library repair via `move_within`: `--under` scoping,
  already-placed rules, idempotent (converges to zero rows), collisions stay put, unrouted
  media exported for the agent.
- **Positional language classification** in the router plus `mlo agent classify --media`
  hints; the explicit `default_language` carries rule provenance (no implicit bucket).
- **`plan dedup-library` / `plan stage-library`** — exact-duplicate content staged out of
  the library with full-SHA-256 confirmation and a curated-copy-wins canonical rule;
  explicit-list staging for triage-judged junk. Both journal index deletions transactionally.
- **EXIF-year photo sorting** (`--exif`) — a stdlib-only, total `DateTimeOriginal` parser
  for JPEG/TIFF; PNG/HEIC return None and route to `Photos/Unsorted/`.
- **Name-pattern distillation** — a deterministic `NAME_PATTERNS` pre-pass plus
  `[classify.name_patterns]` config and `docs/classification-patterns.md`: frontier judgment
  spent once, reusable by a local model.

### Fixed (router-hardening ledger entries)
- **C11–C14** — the router never second-guesses valid existing structure: a filename
  language tag can't outrank the folder (C11), a track under `Music/<Language>/` doesn't
  re-nest (C12), hand-named homes are kept (C13), and `plan_id` is timing-independent (C14).
- **C15–C17** — the conservatism posture made load-bearing: a file under its media root
  stays unless an evidence-backed repair applies; artist/album trees and photo albums are
  never flattened; all root/segment comparisons casefold + separator-normalize; same-basename
  collisions demote to a provenance-flat destination; case-variant occupancy converges.
- **C18–C22** (from the first real 388K-file repair) — cross-type sidecars stay put (C18);
  the **evidence rule** — reorganize moves only toward a home, never to a shelf (C19);
  personal root is pure human placement (C20); the **duplicate-content rule** — fingerprint
  twins stay put (C21); layout roots are structurally un-stageable in `dedup-library` (C22).

## [0.1.0] — 2026-07-06

### Added
- **The safety kernel** (`safeops.py`): the only module that mutates the filesystem;
  protected paths checked on both ends, same-drive staging, never-overwrite, and **no delete
  API anywhere** — enforced by `tests/test_architecture.py` (AST) as CI law.
- **The SQLite store** (`store.py`): append-only op journal, fingerprint index, artifact
  freshness registry (journal-position stamps), run ledger; paths as `surrogatepass` BLOBs.
  CSV/JSON are exported, write-only views (L7).
- **The pipeline**: fingerprint scan (checkpoint/resume) → verdicts
  (ORGANIZED/JUNK/UNIQUE/REVIEW) → `plan organize` / `plan dedup` → idempotent `apply` with
  per-row precondition re-verify, automatic post-condition audit, and residual plans →
  `verify library` → reports (`summary.json`, `suggested_next`, stamped CSVs, stable exit codes).
- **The agent layer**: a deterministic fallback chain with a positionable `local` slot
  (gpt-oss-20b via any OpenAI-compatible endpoint), bounded schema-validated tasks with
  bounded repair, `UNSURE` abstention + escalation, self-consistency voting, and
  `mlo agent eval` against golden sets.
- **The defect ledger** (`docs/defect-ledger.md`) as the contract: every v1–v5 failure and
  the 2026-07-06 closeout discovery maps to a mechanism and a named regression test, enforced
  by `tests/test_defect_ledger.py`.

