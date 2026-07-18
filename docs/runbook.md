# Runbook — consolidating a drive, end to end

The walkthrough below takes one source (an old drive) into an organized library.
Every step is resumable, every mutation is journaled, and nothing here can
delete a file — disposal (the very last act) is a separate, reviewed,
typed-confirmation plan into the OS Recycle Bin / trash (`mlo dispose`, C68),
never a silent side effect.

## 0b. The 2-pass cleanup (the front door)

Most cleanups no longer need the step-by-step loop below — that remains the advanced
path. The product surface is two passes:

```
mlo pilot                      # Pass 1: analyze EVERYTHING (read-only + rehearsed)
mlo serve                      # review the sealed proposal in the browser:
                               #   sections -> clusters -> per-file signals,
                               #   approve/reject, then Execute (typed confirmation)
```

Or headless:

```
mlo pilot
mlo pilot --execute --proposal ".mlo/runs/<id>/proposal.json" --approve-all
mlo verify library --deep      # belt and braces
```

Pass 1 writes one sealed proposal (`mlo.proposal/1`) covering every applicable plan
kind, rehearsed row-by-row, with the critic-judged residue and the honest human queue.
Pass 2 executes exactly the approved sections (approvals are hash-bound — ledger C25),
auto-converges bounded, verifies, and previews what YOU need to dispose (the engine
still deletes nothing, permanently). Anything unresolved lands in the residue queue for
the next `mlo pilot` — the loop is closed but never silent.

## 0. One-time setup

```console
$ mlo init          # writes an annotated mlo.toml next to where you ran it
$ notepad mlo.toml  # set library root, sources, staging roots, taxonomy
$ mlo check         # config + reachability + store health; exit 2 tells you why
```

`mlo check` refuses unreachable enabled sources with the two legal remedies
(reattach the drive, or set `enabled = false`). Dead drives are config data,
never comments.

## 1. Fingerprint

```console
$ mlo scan library      # builds/refreshes the index (resume-fast on re-runs)
$ mlo scan old-drive    # fingerprints the source (full pass each run)
```

Multi-hour scans are normal on spinning disks. The library scan has a stat
fast-path: a re-run re-hashes only files whose size or mtime changed, so
resuming after an interruption is cheap. A source scan re-fingerprints the
whole source every run — by design, since verdicts must reflect current
content — and it is safe to interrupt: a partial scan leaves its artifact
`building`, and every downstream command refuses to consume it until a
complete scan finishes.

## 2. Verdicts

```console
$ mlo verdicts old-drive
verdicts for 'old-drive' (48213 files): JUNK=1201  ORGANIZED=39004  REVIEW=1210  UNIQUE=6798
```

- **ORGANIZED** — fingerprint-identical content already in your library
- **JUNK** — matched an explicit junk rule (yours, in config)
- **UNIQUE** — not in the library; a taxonomy bucket claims it
- **REVIEW** — matched no rule. Nothing touches these without a decision.

## 3. Copy the uniques in (organize), THEN stage the rest (dedup)

The order is enforced, not suggested (ledger L13 — a junk sweep once ran before
the copy phase and quarantined unique files):

```console
$ mlo plan organize old-drive     # refuses (exit 5) if too much is unmatched
$ mlo apply .mlo/plans/plan-organize-old-drive-ab12cd34.jsonl            # rehearse
$ mlo apply .mlo/plans/plan-organize-old-drive-ab12cd34.jsonl --execute
```

Rehearsal and execution are the same code path; the rehearsal re-checks every
row against live disk, so a missing source shows up *before* you execute.
Copies are re-hashed before they count (L15). Interrupted? Run the same apply
again — completed work no-ops (L1).

```console
$ mlo plan dedup old-drive        # duplicates + junk -> E:\Delete\old-drive\...
$ mlo apply .mlo/plans/plan-dedup-old-drive-9f00aa11.jsonl --execute
```

Staging is a same-drive move into `E:\Delete` — instant, reversible, and the
engine will never touch it again except to verify it.

If a run ends `completed_with_residuals` (exit 3), it has already written the
follow-up plan for exactly the rows that didn't complete. Apply it. That loop
replaces the "run it again and hope" convergence ritual.

### One command for many sources: `mlo sweep`

Steps 1–3, across every configured source, as one auditable command — this is the
productized replacement for looping `scan`/`verdicts`/`plan`/`apply` in a shell
script (which is the L0 anti-pattern: an operator ritual that leaves gaps):

```console
$ mlo sweep --confirm-mb 1                 # rehearse: scan+verdict every source,
                                           # report ORG/UNIQ/JUNK/REVIEW + what would stage
$ mlo sweep --confirm-mb 1 --execute       # stage the already-in-library originals out
$ mlo sweep G_Phone1 I_Downloads --execute   # or name specific sources
```

`sweep` refreshes the library index itself, then for each source:
- **holds** any source that still has UNIQUE (only-copy) files — it will not touch
  it, and points you at `mlo plan organize <source>` to preserve them first (a sweep
  must never launder an unvetted only-copy into the curated library);
- stages the ORGANIZED + JUNK originals out, each ORGANIZED file **re-confirmed against
  its library twin at `--confirm-mb` MiB** before it moves (a same-size / same-ends /
  different-middle file is kept in place, never swept off its only unique content);
- writes a `summary.json` with per-source counts and `suggested_next`. Exit 3 means
  at least one source was held (uniques) or kept a file back (failed confirm) — attend
  to those, then re-run. Idempotent: already-staged originals no-op.

## 4. The REVIEW pile

```console
$ mlo agent triage old-drive      # local model recommends, with rationale
```

Or decide by hand from `mlo export source old-drive` (a CSV view). Files whose
disposition is "keep" get organize rules (add a bucket / extend one), then
re-run verdicts + organize. Junk verdicts get junk rules. REVIEW files never
move on their own.

## 5. Verify, then dispose (deliberately, with a typed confirmation)

```console
$ mlo verify library          # fast stat-diff: external edits, residue
$ mlo verify library --deep   # re-fingerprint everything (catches same-size edits)
$ mlo verify staging          # exit 3 + BLOCKING if protected content is inside staging (L12)
```

When `verify staging` is clean and you've lived with the result long enough to
trust it, close the loop with the C68 dispose flow (owner-approved L18
amendment — the OS Recycle Bin / trash, never a true delete):

```console
$ mlo dispose                       # plan: journal-verified staged files only
$ mlo apply "<plan>"                # rehearse
$ mlo apply "<plan>" --execute --confirm-dispose <exact row count>
```

Only files whose path AND current content the journal recognizes as the
engine's own staged output are ever planned; anything else is reported and
left alone. Everything disposed is recoverable through the OS trash UI.
(Deleting the staging roots yourself still works too — mlo itself never
gains a true delete.)

## 6. Repairing an already-consolidated library (v0.2)

For a library that accumulated flat, provenance-named dumps (`Video/old-drive/…`)
next to properly organized trees, `plan reorganize` restructures **in place** —
content-derived Jellyfin groupings instead of source names.

```console
$ mlo scan library                                   # fresh index first
$ mlo plan reorganize --under Video/old-drive --under Photos/old-drive --exif
plan 7d1a4b90c2ff (reorganize, 1841 ops): .mlo/plans/plan-reorganize-library-7d1a4b90.jsonl
  note: in scope: 2210, moves: 1841, already placed: 12, no derivable route (stay put): 357, ...
  357 media files had no derivable identity (they stay put): ...\unrouted.json
$ mlo agent classify --media --paths ".mlo/runs/<run>/unrouted.json"
$ mlo plan reorganize --under Video/old-drive --under Photos/old-drive --hints "<printed hints path>"
$ mlo apply ".mlo/plans/plan-reorganize-library-....jsonl"            # rehearse + review
$ mlo apply ".mlo/plans/plan-reorganize-library-....jsonl" --execute
$ mlo plan reorganize --under Video/old-drive ...                     # converges: 0 ops
```

The hard constraint, enforced three ways, is that **your correct trees cannot move**:
paths outside the `--under` prefixes are never even examined; a correctly-placed file
routes to its own path (already-placed rules cover hand-named-but-valid homes like
`Friends (1994)/Season 05/`); and everything else is an ordinary plan you rehearse
before executing. Files whose identity neither the filename nor the agent can supply
stay exactly where they are — moving them would be guessing.

`plan flatten-provenance` (C27) is the sibling for what `reorganize` can never touch —
device-origin folders like `Documents/E_NAS1/…` or `Audio/G_Phone1/…`, where the
non-media bucket returns no route or the audio hit is C19-blocked. EVERY intermediate
segment matching the provenance pattern strips, at any depth in one pass (C34); the
bucket segment and the non-provenance tree below are preserved. Standard guards apply
(C21 twin skip, L17 collisions skip-and-report, L12 protected refusal, containers
skipped); `mlo pilot` runs it automatically between `date-drain` and `prune-empty`.

### Personal-media provenance drain (P18: C45/C46/C47)

Three narrower gaps `reorganize`/`flatten-provenance` still left, closed together:

- **`plan date-drain` also drains personal VIDEO residue (C45)**, symmetric to its
  existing photo drain: a Video-bucket file sitting in a device folder under
  `layout.personal_root` (`Video/Personal/G_Dashcam/…`) — anything not already at
  `personal_root/<Year>/…` — lands at `personal_root/<Year>/<filename>`. Year
  precedence (2026-07-15 live-data fix): a STRONGLY-structured NAME date
  (WhatsApp `VID-YYYYMMDD-WA####`/`IMG-YYYYMMDD-WA####`, or a leading 14-digit
  device stamp — `imgclass.structured_name_year`) is checked FIRST and wins
  over the video's own embedded creation date (the MP4/MOV `mvhd` atom,
  `vidmeta.creation_year`), because a WhatsApp re-encode writes a bogus
  constant mvhd date while the filename the device wrote is trustworthy; the
  mvhd date is checked next, then a looser name-embedded epoch-ms timestamp —
  never the filesystem mtime (a copy resets it — C19). No date signal
  anywhere means the file drains to a holding shelf,
  `personal_root/Undated/<filename>` (rule `route:personal:undated`) — it
  drops the device-name provenance without inventing a false date, rather
  than staying stuck in the device folder forever. `route()` never applies
  here by design (everything under `personal_root` is pure human placement,
  C20), so the drain's own scope check does the guarding.
- **Collision disambiguation (C46)**: `plan reorganize` and `plan date-drain` accept a
  `disambiguate` flag. Off by default (every existing skip-and-report plan is
  unaffected); `mlo pilot` turns it on for the media drains. When two content-DISTINCT
  files (never byte-identical — that stays a dedup decision) want the same destination
  — two devices each holding a `Bhaja Govindam.mp3`, a `PTT-…-WA####.opus` voice note
  with the same WhatsApp-generated name — both survive, tagged with the source's own
  immediate provenance folder: `Bhaja Govindam [E_NAS1].mp3` /
  `Bhaja Govindam [G_OldThumbDrive].mp3`. The tag is computed at plan time from data
  intrinsic to the file (never a `(1)/(2)/(3)` counter — that IS the v1 L1 disaster);
  re-running the plan always produces the same tags.
- **`plan flatten-provenance` reaches deeper into curated trees (C47)**: a provenance
  segment sitting right at a media bucket's top (`Audio/I_SSD1/…`) is still left
  alone in full (the C28 boundary — audio/photo-triage's gap, not flatten's to launder).
  But a provenance segment DEEPER inside an already-curated root
  (`Audio/Music/Classical/E_NAS1/…`) now strips, landing at
  `Audio/Music/Classical/…` — the file was already triaged into that subtree, and the
  device folder is a proven interloper. `personal_root` is deliberately excluded
  from this deeper-strip set (2026-07-15 fix): a provenance folder under
  `Video/Personal` is left alone by flatten entirely — draining it (by year, or
  to the `Undated` shelf) is C45 date-drain's job, not flatten's.

```console
$ mlo plan date-drain --under Video/Personal                 # C45: drains device video dumps by year
$ mlo plan reorganize --under Audio                           # C46 off by default: collisions skip-and-report
$ mlo pilot                                                    # C46 on: media drains disambiguate automatically
$ mlo plan flatten-provenance --under Audio/Music              # C47: strips device folders inside Music
```

Two CLI-only movers round out the set: `plan bad-archives` stages archives that fail
an integrity check (bad magic, truncated zip directory, empty — C37; deliberately not
in the pilot) and `plan relocate --map <json>` moves an explicit relpath→dest mapping
(the surgical repair tool — e.g. restoring journal-traced mismoves).

`plan containers` (C33) handles what neither of the above may touch: **semantic
containers** — subtrees whose folder name declares their meaning as a unit (a
`Phone Backups/` snapshot, a `D drive/` image, an `User1Backup/` export).

For **phone backups** the destination is DEVICE-KEYED — `Backups/Phones/<device>`
where `<device>` is `S5`, `S4`, `Nexus6`, `iPhone11Pro`, etc. — the model IS the
identity, and every accidental wrapper name (`Phone Backups/`, `CellPhone Backups/`,
`User1Backup/`) plus every owner segment (`user1/`, `User3/`) is dropped. Scattered
backups of the SAME device from different sources merge into ONE tree: files with
byte-identical contents dedup (one survives, the rest silently skip); files that
genuinely differ get an owner discriminator (`Backups/Phones/S5/user1/Phone/… vs
.../user2/Phone/…`) so both are preserved and the source is explicit. `S4backup/` and
`s4Backup/` both normalize to `S4`.

For **drive images and app backups** the destination keeps the container-name scheme
(`Backups/Drives/D drive/…`, `Backups/Apps/ProjectBackup/…`) since there's no
finer-grained identity to key on.

L12 protected refusal is whole-plan; the pilot runs `containers:library` first among
library sections. Built-in patterns cover phone/drive/app backups; extend with
`[containers.patterns]` and `[containers.homes]` in config — always the pair (a
patterns kind with no home is a config error):

```toml
[containers.patterns]
dcim-roll = ['(?i)^dcim$']
[containers.homes]
dcim-roll = 'Backups/Phones'
```

## Books (P17/C43)

`Ebooks` is a taxonomy bucket (`.epub .mobi .azw .azw3 .azw4 .prc .lit .fb2 .djvu`)
routed to a new top-level `Books\` root: `Books\<Last, First>\<Series>\<NN - Title>.ext`
when both author and series are known, `Books\<Last, First>\<Title>.ext` for an
author with no series, and `Books\Unsorted\<original name>` when no identity is
derivable — never a guessed folder. Identity comes from embedded epub OPF / mobi
EXTH metadata first, a pure filename parse second.

Title-only or ambiguous names (no embedded metadata, no parseable author) get
identified by a subagent-hints workflow, not an engine LLM call:

```console
$ mlo pilot                              # Pass 1 review-set includes unidentified Ebooks
                                          # (route:book:unsorted rows)
# a Claude Opus subagent judges each ~1,000-file review-set batch,
# emitting a hints-JSON fragment ({book_author, book_title, book_series, book_index})
# batches are merged and validated against hints.load_hints before use
$ mlo pilot --hints <merged-hints.json>  # re-plans; Unsorted books with a resolved
                                          # author reshelve (route:book:reshelve)
$ mlo serve                              # owner reviews the sealed proposal, as usual
```

Jellyfin's Bookshelf plugin reads epub/mobi/azw/pdf directly from this
author/series layout; `.lit`/`.rtf` organize correctly alongside them but won't render.

## Cheat sheet

| Exit | Meaning | Typical remedy |
|---|---|---|
| 0 | done | — |
| 1 | unexpected error | read the message; file an issue |
| 2 | config/plan refused | the message names the key or gate |
| 3 | completed with residuals / blocking findings | apply the residual plan / resolve findings |
| 4 | stale input | run the named rescan |
| 5 | coverage blocked | add taxonomy rules for the named tokens |
