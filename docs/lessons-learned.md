# Lessons learned — the five-pipeline story this project distills

This is the narrative companion to [defect-ledger.md](defect-ledger.md). The ledger maps
each failure to the mechanism that kills it; this document tells the story in order, with
real numbers and absolute dates, because the design of `mlo` makes no sense without it.

The setting: years of personal data scattered across four drives — old system images,
family phone backups, a 4TB NAS dump, camera rolls, a web-scrape archive, recovery-tool
output — to be consolidated into one organized, deduplicated library (`I:\Organized`).
About two terabytes and, by the end, 388,609 files. The work ran from June 6 to July 6,
2026, across five pipeline versions and a closeout. Every version existed to clean up a
defect class the previous one permitted.

---

## v1 (early June 2026): the double-run

The initial pipeline scanned three source drives, fingerprinted everything, copied
182,592 unique files (1.517 TB) into the library, and restructured 459,233 files into a
media-server taxonomy. It worked — once.

Phase 3 had no completion checkpoint. It ran twice. On the second pass, its collision
resolver treated the first run's output as pre-existing files and dutifully suffixed
every copy: `movie (1).mkv`, `movie (2).mkv`, `movie (3).mkv`. Result: **~165,000
duplicate files, ~258 GB of wasted disk**, and an entire follow-up version (v2) whose
only purpose was to undo one missing idempotency guard.

The deeper lesson wasn't "add a checkpoint." It was that the collision resolver invented
destination names *at execute time* — so re-running anything could always manufacture new
state. In `mlo`, destinations are fixed at plan time and an occupied destination is
drift, reported and skipped. The engine cannot express the v1 failure.

## v2 (June 6–7, 2026): fourteen runs to converge

The dedup fix scanned for suffix artifacts, verified identity (size → 64 KB quick compare
→ full SHA-256), and staged confirmed duplicates. It eventually freed the 258 GB — after
**14 dedup runs and 11 cleanup runs**, manually re-invoked until they reported zero
changes, because convergence was an operator ritual rather than a mode.

Two bugs were caught only by ad-hoc review before execution: a crash in the report
writer, and a regex blocker — `\d+` inside parentheses matched *years*, so
`Movie (2007).mkv` classified as a duplicate suffix. One character class away from
mass-misclassifying a library. That regex now lives in this repo's property-based test
corpus permanently, along with the habit it taught: parsers get property tests, not
one example.

v2 also validated an idea worth keeping: at execute time it re-verified every file's
hash before acting, and correctly refused 28 files whose content had diverged (different
encodes of the same title). Execute-time precondition re-verification became a load-
bearing part of the `mlo` plan/apply contract.

## v3 (June 7, 2026): the missing keyword

The v1 restructurer classified media by language keywords. The keyword list was missing
one entry: **'English'**. Every English-language file — the majority of the library —
silently routed to the `Other` bucket. By the time anyone looked, `Movies/Other` held
4,440 misfiled folders.

Nothing crashed. No error was logged. The classifier did exactly what it was told, and
what it was told was wrong in a way only coverage accounting could reveal. The fix took
a dedicated version: 116 movies, 755 TV episodes, and 216 artists reclassified across
1,154 moves, backed by 323 new tests.

In `mlo`, classifiers are total functions with an explicit `UNMATCHED` outcome, there is
no implicit catch-all bucket, and a plan build **fails** when the unmatched fraction
crosses a threshold — with the unmatched items ranked by token frequency, so a missing
'English' would name itself at the top of the report.

## v4 (June 8, 2026): "complete," then two fix scripts

v4 was the big library restructuring: TV-show folder merges, a 2,087-folder triage down
to under 100, artist deduplication, fuzzy near-duplicate detection, 13,281 photos sorted
by EXIF year — 10,705 recorded moves. The run was declared complete.

Then came `post_v4_fixes.py` (+375 moves) and a provenance-correction pass (+23 moves),
because verification was a separate phase that happened after the declaration, and it
found what the run had missed. "Complete" had meant "the script finished," not "the
post-conditions hold."

`mlo` attaches the audit to the run: apply ends with an automatic post-condition check,
and if residuals exist the run's status *is* `completed_with_residuals` (exit 3) and a
residual plan is emitted. A run that isn't done tells you so and hands you the remainder.

## v5 (June 12–22, 2026): fingerprints, and the traps of derived state

v5 was the strongest version: content fingerprints (size + SHA-256 of first and last
128 KB), a library index used as a membership filter, four verdicts per source file
(ORGANIZED / JUNK / UNIQUE / REVIEW), long-path handling, per-row CSV durability with
torn-row repair, checkpoint/resume. Round 1 consolidated 10 candidate folders and copied
13,279 unique files. Round 2 staged 237,209 duplicates.

Its failures were all *state* failures:

- **Two copies of the candidate list** lived in two scripts. They diverged; one round of
  candidates was reviewed but nearly missed consolidation.
- **Stale derived CSVs were consulted as truth.** A drive-space analysis quoted 693 GB
  from a scan file that predated the consolidation run; the drive actually held 278 GB.
  The library index itself went stale after a copy phase and had to be refreshed via a
  human-remembered sentinel-file ritual.
- **A formatted drive lived on in the code** as a commented-out entry — until a run where
  it wasn't, producing 3,560 phantom "source not found" errors in the logs.

`mlo`'s answers: one config file that every stage reads (no lists in code); derived
artifacts stamped with a journal position, consumers refusing stale inputs by default,
and the index updated in the same transaction as the mutation; and config validation that
makes an unreachable-but-enabled root a hard error — a dead drive must be explicitly
`enabled = false`, visibly, in data.

## The closeout (July 6, 2026): everything the paperwork didn't know

The final pending steps were executed under audit discipline: trust nothing in the status
document, recompute every claim from primary artifacts (verdict CSVs, operation logs,
live disk), machine-check the end state. The status document turned out to be wrong in
four places, and the disk held surprises the pipeline had never been told about.

- **The "pending" cleanup script had already run** — and in the documented (unsafe)
  order, so two files with UNIQUE verdicts had been quarantined into a staging folder by
  a hardcoded junk list *before* the copy phase could preserve them. Disposal would have
  destroyed them. They were restored, copied, verified, and re-staged.
- **81 GB stood stranded on a drive root** because a mover's "destination exists → skip"
  guard had silently collided with a folder the earlier phase pre-created. Skipped, and
  nobody was told. In `mlo`, "skipped" is a first-class reported state.
- **A BlueStacks backup was found *inside* a disposal target** — content protected by an
  absolute never-touch rule, sitting in a folder scheduled for deletion. The engine now
  checks protected-path rules on *both ends* of every operation and re-scans staging
  roots before anything irreversible.
- **The big one: 3,858 unique files had been manually moved into staging** — someone
  tidied a leftover folder into `\Delete` with Explorer, outside the pipeline, at a
  slightly different nested path. The operation log proved the pipeline hadn't done it;
  fingerprint checks proved the content existed *nowhere else on any drive*. Disposal
  would have silently destroyed all of it. Every file was recovered by basename +
  fingerprint search of the staging tree, copied into the library, and verified. One
  more file from a family member's phone backup was recovered the same way from a
  *different* drive's staging (an identical copy, proven by fingerprint); two files
  (6.6 MB of WhatsApp media) were proven unrecoverable — deleted outside the pipeline,
  documented as exceptions rather than papered over.
- **The verifier itself got fooled, instructively.** It reported 19 copy mismatches.
  All 19 were false: the pipeline's log recorded the *intended* destination for
  skip-as-identical rows even when the identical twin actually sat at a suffixed slot.
  Probing every slot resolved all 8,032 such rows — 8,013 at the base path, 19 at a
  suffix, zero unresolved. Manifests must record **resolved** state, never intended
  state.

The closeout finished with six machine-checked invariants — every unique disposition
proven, protected folders byte-identical to baseline, all swept roots empty, the index
recount landing on the manifest-predicted 388,609 exactly — and stopped at a human gate
before disposing of 844,941 staged files (~2.34 TiB), because destroying the only
rollback copies is a decision, not a step.

---

## What this project takes from all of it

1. **Safety must be constructed, not practiced.** Every guard that lived in a runbook,
   a comment, or an operator's memory eventually failed. The guards that survived were
   the ones code enforced. Hence: one kernel, no delete API, an AST test as CI law.
2. **Primary artifacts or it didn't happen.** The status doc was wrong four times; the
   logs, manifests, fingerprints, and the disk itself were wrong zero times. Every
   verification in `mlo` recomputes from the journal and the filesystem.
3. **Record resolved state.** A manifest that logs what you *meant* to do is a trap for
   every future reader, human or model.
4. **Humans will move files outside your pipeline.** Not maliciously — helpfully. Plans
   must re-verify preconditions at execute time, and staleness must be detectable, or
   drift becomes data loss.
5. **A dry-run that skips precondition checks is theater.** The v5 dry-run previewed
   15,878 copies, errors=0; the execute found 3,859 sources missing. Rehearsal and
   performance must share the same checks — in `mlo` they share the same code path.
6. **Uniqueness is not worth.** 8,032 of the "unique" files were identical scrape
   artifacts under different paths; 45 GB of "review" material was a redundant archive
   of content already preserved. Deciding what deserves to live in a library is a
   judgment task — bounded, verifiable judgment — which is exactly the job of the agent
   layer, and why it exists.

## Postscript: v0.2, or "lift and shift is not organization"

v0.1 shipped with provenance-flat placement — `Video/<source>/<original path>` — and the
owner's review of the result named the gap precisely: *"organization by definition
includes logic, structured, meaningful groupings. Having `Videos/G_Dashcam/` and
`Videos/I_Movies/` is not very meaningful."* Correct. Moving files safely is table
stakes; the product's job is deciding where they *belong*.

v0.2 answered with the hierarchical router: `Movies/<Language>/Title (Year)/`,
`TV_Shows/<Language>/<Series>/Season NN/`, photos by EXIF year — Jellyfin-compatible by
default, seeded from the layout the real library had already proven across five pipeline
generations. Two lessons earned their own ledger entries within hours of writing it:

7. **The router must never second-guess valid existing structure.** The first
   implementation would have moved a correctly-placed Tamil film to `English/` because
   its filename said `.English.Subs`, nested `Music/Tamil/Tamil/`, and renamed a
   hand-named `Friends (1994)` series folder. Idempotence — a correctly-placed file
   routes to itself — is not a nicety; it is the entire safety contract of in-place
   repair, and it must be property-tested, not assumed (ledger C11–C13).
8. **Identity must never be derived from timing.** A plan's identity hashed its creation
   timestamp, so "an identical rebuild is the same plan" was true only within the same
   second (C14). Semantic identity and file integrity are different facts and get
   different hashes.
