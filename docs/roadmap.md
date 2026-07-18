# Roadmap — deferred features, each with its trigger

Per the harness rule (§8): defer means *document with an explicit trigger, don't build*.
Nothing below lands until its trigger fires. This keeps v0.1 a small, correct core whose
guarantees are non-negotiable while features stay negotiable.

## v0.1 — shipped when done (the current scope)

- Config (`mlo.toml`) + startup validation: unknown keys refused, unreachable enabled
  roots refused, explicit `enabled = false` for dead drives.
- SQLite store: append-only operation journal, fingerprint index, artifact freshness
  registry (journal-position stamps), run ledger. CSV/JSON as exported views only.
- Safety kernel (`safeops.py`): protected paths checked on both ends, same-drive
  staging, never-overwrite, **no delete API**, dry-run and execute on one code path;
  `tests/test_architecture.py` enforces the boundary by AST.
- Pipeline: fingerprint scan (checkpoint/resume) → verdicts (ORGANIZED / JUNK / UNIQUE /
  REVIEW) → `plan dedup` / `plan organize` (extension buckets, structure-preserving) →
  idempotent `apply` with per-row precondition re-verify, automatic post-condition
  audit, residual plans → `verify library` lint → reports (`summary.json`,
  `suggested_next`, stamped CSVs, stable exit codes).
- Agent layer: deterministic fallback chain with a positionable `local` slot
  (gpt-oss-20b via any OpenAI-compatible endpoint), bounded tasks (`classify`,
  `triage`, `run`), schema-validated outputs with bounded repair, `UNSURE` abstention +
  escalation, self-consistency voting, per-call ledger, kill-switch, and `mlo agent
  eval` against golden sets.

## Shipped in v0.2 (2026-07-06)

Organization now means meaningful groupings, not provenance dumps (user directive:
"having `Videos/G_Dashcam/` is not very meaningful"):

- **Jellyfin naming as the default** — `Movies/<Language>/Title (Year)/`,
  `TV_Shows/<Language>/<Series>/Season NN/`, config-seeded from a real, proven library
  layout (`[layout]` + `[layout.languages]`). Strict parsers in `naming.py` are the L3
  answer (years only parenthesized+plausible; property-tested; total).
- **`plan reorganize`** — in-place library repair via `move_within`: `--under` scoping,
  already-placed rules, idempotent (converges to zero rows), collisions stay put,
  unrouted media exported for the agent.
- **Language classification** — shipped as positional path-token detection in the
  router plus `mlo agent classify --media` hints; the explicit `default_language`
  carries rule provenance (no implicit bucket).
- **`plan dedup-library` / `plan stage-library`** (v0.2.x, driven by the first real
  repair) — exact-duplicate content staged out of the library with full-SHA-256
  confirmation and a curated-copy-wins canonical rule; explicit-list staging for
  triage-judged junk. Both journal index deletions transactionally.
- **EXIF-year photo sorting** — shipped WITHOUT a dependency: a stdlib-only, total
  `DateTimeOriginal` parser for JPEG/TIFF (`--exif`). TIFF-based RAW (`.dng`, `.kdc`,
  `.cr2`, `.nef`, `.arw`, …) is in the starter `Photos` bucket and reads years through
  the same path (the parser keys on magic bytes, not extension); a year-attested RAW
  routes to `Photos/<year>/`, a yearless one stays put under the C19 evidence rule
  rather than being laundered to a shelf. PNG/HEIC still return None and route to
  `Photos/Unsorted/` (a real HEIC parser would need a dep — still deferred).
- **Magic-byte content sniffing** (`sniff.py`, stdlib-only, total, read-only) — the
  answer to the false-carve pile: a recovery blob written into a `.swf`/`.dat`/`.au`
  whose extension lies is routed by what it IS. `mlo plan reorganize --sniff` reads the
  leading bytes of in-scope files that have **no** taxonomy bucket, and an identified
  video/audio/image carve routes to `<MediaType>/Unclassified/` — an evidence-backed
  reclassification into a holding pen a critic or the human then places, never a guess at
  the specific home. Content is consulted **only** when the extension yields no bucket, so
  it can never override the user's taxonomy; a headerless blob honestly gets no hint and
  stays put. The sniffed kind flows through the router's existing `Hints` seam, exactly
  like an EXIF year.

## Shipped 2026-07-10 (unreleased, toward 0.3) — the 2-pass product

The whole-library cleanup collapses from ~43-46 manual invocations to TWO passes:

- **`mlo pilot`** (Pass 1) — one command analyzes everything read-only + rehearsed:
  scan, per-source verdicts, every applicable plan builder (organize; dedup gated
  behind organize per L13; dedup-library, reorganize, date-drain; prune-empty as a
  preview), deterministic hints (EXIF/sniff), the full-signal review-set (CANONICAL),
  a critic panel on the library's unrouted residue (`--chain` frontier override,
  `--critic-limit` bound, overflow queued for the human), a hinted re-plan, and a
  per-section rehearsal — assembled into ONE sealed proposal (`mlo.proposal/1`).
- **`mlo pilot --execute`** (Pass 2) — approvals (`mlo.approvals/1`) are hash-bound to
  the reviewed proposal (ledger C25: approve-X-execute-X made mechanical); approved
  sections execute in dependency order with bounded convergence; verify tail; disposal
  preview. No model calls in Pass 2.
- **`mlo serve`** — the localhost web UI over the same artifacts: launch Pass 1, review
  the proposal (sections -> clusters -> per-file signals + critic rationale), approve/
  reject, execute with typed confirmation, watch convergence, read the final report.
- **`[llm] critics_chain` / `agent critics --chain`** — the critic panel runs on a
  stronger chain than routine tasks (the heaviest judgment gets the best model).

## Shipped 2026-07-11 (unreleased, toward 0.3) — semantic containers (C33)

The four-way subtree triage: curated tree (stay) → **container (move WHOLE to its
kind's home)** → dump (flatten wrapper) → loose files (route per-file). New pure
`containers.py` matcher (built-in kinds `phone-backup`/`drive-image`/`app-backup` +
`[containers.patterns]`/`[containers.homes]` config extensions), `plan containers`
builder (device-keyed phone homes, merge-by-identity, byte-identical dedup,
owner disambiguation on content clashes — D10-D12; the earlier whole-unit-defer
D5/D6 scheme is subsumed), the `route:container:member` guard, and a
`containers:library` pilot section that runs first among library builders.
Hardened 2026-07-13 (C39): the container HOME is durable — `root_of()` claims
any `<home>\<ident>\…` path, so consolidated snapshots are permanently
invisible to reorganize/flatten/dedup and to build_containers' own patterns.

## Shipped 2026-07-12/13 (unreleased, toward 0.3) — the P13-P15 wave

C34 any-depth provenance flatten; C35 non-media bucket routes (Presentations/
Spreadsheets/Archives/Installers) + Comics series normalization; C36 sidecar
handling (subtitles/posters/nfo follow their media anchor) with C38/C40/C41
corrections (sidecars follow only anchors that actually move; same-bucket
same-stem siblings are alternate copies, never sidecars); C37 bad-archive
detection (`plan bad-archives`, CLI-only by design); C39 durable container
homes; the P15 super-review hardening sweep (store schema stamp + corrupt-db
remedy, crash-reconcile rmdir/audit corrections, upfront approvals validation,
web UI session-token/Host guard + single-mutator gate, config type refusals);
and **C42/P16 — Pass-1 convergence rehearsal**: the proposal now seals the
projected END-STATE (the whole cycle chain rehearsed against an in-memory index
copy), so the human approves what actually executes instead of just the first
cycle, with a per-section `convergence_delta` audit for any execute-time
index-vs-reality divergence.

## Shipped 2026-07-15 (unreleased, toward 0.3) — Ebooks Phase A (P17/C43)

A real `Ebooks` bucket and `Books\` top-level root, organization-first (Jellyfin
is one consuming endpoint, not the goal — `.lit`/`.rtf` books must organize
correctly even though Jellyfin can't render them): new `bookmeta.py` (pure
epub OPF / mobi EXTH readers, filename parser, particle-aware "Last, First"
shelf-author normalizer, Windows-safe segment sanitizer), config surfaces
(`ebooks_root`, both shipped templates), hints plumbing (`book_author`,
`book_title`, `book_series`, `book_index`), the `route:book:*` router family
(author/series placement, reshelve-on-new-identity, honest `Books\Unsorted`),
and metadata.opf/cover sidecar following. Identity for title-only/ambiguous
books is judged **out-of-engine** by Claude Opus subagents against the
existing `--hints` surface — no `[llm]` config, no engine LLM call for this
feature. This covers B1 (format bucket) + B2 (embedded/filename/hinted
identity); B3 below is the deferred judgment tier for ambiguous formats.

## Deferred

| Feature | What it is | Trigger to build it |
|---|---|---|
| Container critic tier (C33 phase 2) | A container panel judging candidate subtrees the patterns didn't match (rolled-up signals: type histogram, date range, fanout, sample names), via a `container_critic_spec` on `run_panel`'s machinery; judged mappings feed `build_containers` | Pattern tier leaves a real pile of unclassified coherent subtrees on the live library that the human queue can't keep up with |
| Container content-signature detection (C33 tier 3) | Deterministic mixed-type/marker-file heuristics (`.vcf`+`.db`+images = phone snapshot; `Program Files`+`Users` = drive image) | A live container repeatedly evades folder-name patterns AND the critic tier |
| Enrichment write-back (gated kernel op) | Writing a fetched `.nfo`/poster sidecar, or embedding a corrected ID3/EXIF tag, into the library | The connectors (`mlo.enrich.*`) already FETCH/PARSE/RENDER; writing is a filesystem mutation and therefore the kernel's exclusive job. A new never-overwrite `write_sidecar` op (sidecars) and a copy→embed→verify→`hashdrift.recompute` path (embedding) land only behind an explicit boundary review — the safety kernel is non-negotiable. `hashdrift.recompute` is already built and tested for when it does |
| HEIC/PNG date extraction | EXIF-equivalent dates for formats the stdlib parser can't read | A real pile of HEIC photos routes to Unsorted; ships as an isolated extra (`mlo[photos]`), never in core |
| ~~OpenSubtitles connector~~ | **Search shipped (P21/C58):** `enrich/subs.py` is now the OpenSubtitles REST v1 SEARCH connector (metadata only; the SubDB code is deleted). Still deferred: downloading/writing the actual `.srt`, which needs a gated kernel path | Fetch/write waits on a real use case + the kernel write design |
| EXIF-year persistence cache | Store each photo's EXIF year in the index so `pilot` re-runs stop re-reading up to ~1 MiB/file | Re-analyze latency on a large photo library becomes the bottleneck (measured in the P15 review) |
| Fuzzy near-duplicate detection | Edit-distance + size + hash clustering (report-only, human-reviewed) | Post-v0.2; requires a review workflow design first — the predecessor produced 2,601 clusters and no good way to act on them |
| ~~Convergence automation (`--until-stable`)~~ | **Shipped (2026-07-10), absorbed into `mlo pilot --execute`**: bounded convergence (residual retry + builder re-plan, max 3 cycles, idempotent library builders only, sticky rejections). The trigger fired — a realistic full cleanup measured ~43–46 manual invocations | — |
| ~~Sidecar handling~~ | **Shipped (2026-07-12, C36; corrected by C38/C40/C41)**: .nfo/.srt/poster/AlbumArt files follow their media anchor when it moves — and only when it actually moves | — |
| Embedding-based similarity | Semantic near-dup / content clustering | The prompt-based agent layer proves insufficient on the eval sets — not before |
| Agent autonomy levels | Orchestrator ranges from suggest-only to auto-apply-nondestructive | `mlo agent eval` accuracy/abstention thresholds met and published in [agent-design.md](agent-design.md) |
| Non-Windows *supported* targets | Linux/macOS as first-class (code is already portable; CI runs Ubuntu) | CI green on Ubuntu for two consecutive releases **and** a real non-Windows user |
| Full-hash verdict escalation | Escalate `(size, quick_hash)` matches to a whole-file SHA-256 before calling a file ORGANIZED | A false-ORGANIZED (same size + same first/last 128 KiB, different middle) is observed in practice. Mitigated today by reversible staging + human disposal + `verify --deep`; `fingerprint.full()` already exists to slot in |
| Chunked journal commits | Batch ~100 op intents/done-marks per fsync instead of per-op | The per-op double-commit (durable but two fsyncs/op) proves too slow on a real spinning-disk run. Correctness-preserving optimization only — the reconciler already tolerates a wider window |
| Reparse-point / short-name canonicalization | Resolve junctions, symlinks, and 8.3 short names (`BLUEST~1`) to their true target before the protected-substring check | A same-drive junction/short-name into a protected tree is demonstrated to slip the name-based guard. Narrow today (same-drive only; `os.walk` yields long names), so deferred behind `os.path.realpath` in `PathPolicy.check` when a real case appears |
| Ebooks Phase B — judgment tier (B3, C43) | `.pdf`/`.doc`/`.docx`/`.rtf`/`.txt` are NEVER auto-routed as books (embedded metadata is untrustworthy or absent — typist ≠ author, "Document1" is not a title); a conservative pre-filter builds a review-set, Opus subagent batches (~12 × 1,000 files, same protocol as Phase A) judge book-or-not plus identity, human-gated hints re-plan | Phase A's B1+B2 buckets are live and the owner wants the ~11,600 ambiguous-format residue actually triaged, not just left alone |
| In-engine `book_critic_spec` (cloud chain) | A `[llm]`-driven critic spec for Ebooks identity, so routine batches don't require a manual Opus-subagent dispatch each time | The out-of-engine Opus-subagent protocol (Phase A) proves the judgment quality but the manual batch-dispatch/merge cycle becomes the bottleneck on repeat runs |
| Cross-format book dedup | The same title held as `.epub` + `.mobi` (or other multi-format twins) currently coexist under the same author/series — not byte-twins, so C21 doesn't apply, and no format-preference rule decides which (if any) is "the" copy | Explicit non-goal today (owner call, P17): a human decision about format preference, not a heuristic. No trigger scheduled — revisit only if the owner asks |

## Known limits (honest)

- **Protected-path matching is by resolved-string substring**, not by canonical target:
  a directory junction or 8.3 short name pointing into a protected tree can evade it on
  the same drive (see the reparse-point roadmap row). Cross-drive is already refused.
- **Freshness tracks engine mutations**, not concurrent external writers during
  `apply --execute` (single-mutator assumption). External edits between runs are caught by
  `verify` and by execute-time re-verification, not prevented.
- **Fingerprint is `(size, head128K, tail128K)`** — collision-resistant enough for
  reversible staging decisions, not a cryptographic content identity (see full-hash row).
- **The router derives *placement identity* from paths, not content** (v0.2): a TV series
  name comes from the filename before `SxxEyy` (existing series folders are respected, not
  re-derived); EXIF reading covers JPEG/TIFF only; a file whose identity neither the path
  nor a hint supplies stays put — by design, never by guess. Content sniffing (`--sniff`)
  is the one content-derived signal, and deliberately narrow: it only reclassifies a
  false-carve into a media-type *holding pen*, never names the specific title/home.

## Explicit non-goals

- **Deleting user files.** True deletion stays impossible; `mlo dispose` (C68) covers
  staged duplicates via the OS Recycle Bin / trash behind a typed confirmation. Beyond
  that, disposal stays a human act outside the
  engine, permanently. Not a roadmap item.
- **Cloud storage backends.** This is a local-drives tool; sync services have their own
  dedup.
- ~~**A GUI.**~~ *Amended by owner directive (2026-07-10).* The boundary that SURVIVES:
  visual surfaces are **consumers of sealed artifacts** (proposal/summary/plans) with
  zero filesystem power — `mlo serve` ships in-tree on exactly those terms (localhost
  only, every mutation still flows through the gated plan/apply pipeline, execute behind
  an explicit typed confirmation). What stays a non-goal is a GUI with its own engine
  authority.
