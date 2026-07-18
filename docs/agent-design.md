# Agent Design — high-quality work from a small local model

The agent layer's design constraint, stated bluntly: **a ~20B local model
(default: `gpt-oss:20b` on Ollama) must produce work you can trust for file-disposition
decisions over personal data.** Cloud models may sit in the chain, but the architecture
never *requires* them — your filenames and folder structure are the most intimate index
of a life, and they shouldn't have to leave your machine to get organized.

Small models fail in known ways: they drift off-format, they guess when unsure, they
lose the plot in long contexts, and they're confidently wrong at a higher base rate.
Every element below exists to convert one of those failure modes into a non-event. The
kernel already guarantees the model *cannot damage data* (no delete API, human-gated
disposal — see [architecture.md](architecture.md) §1); this document is about making its
*judgment* reliable enough to be useful.

## 1. The fallback chain

Deterministic, config-declared, tried in exact order, one attempt per entry, no
auto-discovery:

```toml
[llm]
enabled = true
chain = ["local", "claude-haiku-4-5", "gemini-2.5-flash"]

[llm.local]                    # the `local` slot; skipped when enabled = false
enabled = true
url = "http://localhost:11434" # any OpenAI-compatible endpoint (Ollama, LM Studio, ...)
model = "gpt-oss:20b"
num_ctx = 8192
timeout_s = 240                # local models are slow; don't punish them for it
keep_alive = "30m"             # a 13-GiB cold reload mid-batch looks like an outage
reasoning_effort = "medium"    # for thinking models (gpt-oss levels)
```

Semantics (proven in an earlier project of the same owner, generalized here):
- `local` is a **positionable slot** — put it first for privacy-first/no-quota, or last
  as the offline fallback. When `[llm.local].enabled = false` the slot is skipped, not
  errored.
- One attempt per entry; failure/timeout advances the chain. No retries inside an entry
  (retries belong to the *repair* protocol, §3, which is about output quality, not
  transport).
- Keys come from environment variables only (`MLO_ANTHROPIC_KEY`, `MLO_GEMINI_KEY`;
  hosted OpenAI-compatible entries use `MLO_OPENAI_KEY` and optionally
  `MLO_OPENAI_BASE_URL`); config files never contain secrets, and the Gemini key travels
  in a header, never the URL.
- `[llm] enabled = false` is the kill-switch: every `mlo agent` command refuses with a
  clear message and exit 2 — including a live `mlo agent eval`. The one deliberate
  exception is `mlo agent eval --mock`, which exercises the deterministic heuristic
  endpoint and needs no model at all (that's what CI runs).
- Every reply carries its chain ledger (entries tried, outcome, latency, fallback hops)
  in memory for the caller, and `mlo agent eval` **persists it** to
  `agent-ledger.jsonl` in the run directory (a write-only view, same discipline as the
  CSV exports — nothing reads it back) with a rollup (`chain_ledger`: calls answered,
  entry mix, fallback hops, average latency) on the run's `summary.json`.
- Adapters (OpenAI-compatible, Anthropic, Gemini) are stdlib `urllib` JSON POSTs — no
  streaming, no SDK, zero runtime dependencies preserved.

**Critics chain (`[llm] critics_chain`, `--chain`).** The critic panel carries the
heaviest judgment in the system (file-disposition hints), so it may run on a stronger
chain than routine tasks: resolution is CLI `--chain` > `[llm] critics_chain` >
`[llm] chain` (`agent/llm.py::chain_config`). The `[llm] enabled` kill-switch is never
overridden, and an override cannot wake a disabled local slot. The Q4-local-fidelity
bar remains the DESIGN target — prompts/schemas stay small-model-honest — but a run
may buy better judgment when the owner points it at a frontier entry.

## 2. Bounded tasks, never open-ended agency

Every LLM call in `mlo` is one of a small set of **task shapes**, each defined by:

- a few-shot prompt template,
- a JSON schema for the reply,
- an **enumerated option space** — the model picks from choices the engine provides
  (taxonomy labels from config, `suggested_next` CLI strings from `summary.json`); it
  cannot invent an operation, a label, or a path,
- a validator, and an escalation policy.

The model never sees raw file listings. The engine **pre-digests deterministically** —
extension histograms, folder rollups, token frequencies, size distributions, sampled
exemplars — so every prompt fits an 8K context *regardless of library size*, and the
model reasons over evidence summaries rather than 100K-line dumps. (This is how a human
expert actually triages a 102,449-file REVIEW pile: by the shape of the data, not file
by file.)

**CANONICAL — critics judge with ALL signals, never a brittle regex** (owner directive,
2026-07-09). Deterministic patterns are only high-confidence pre-filters for tested
conventions (WhatsApp names, `VTS_*` DVD structure); everything ambiguous goes to a
critic, and the critic's item must carry every signal a human would read after opening
the file: the full path, siblings in the folder, embedded document properties
(`docmeta`/`ole` -- creator, internal title, company, dates), size, mtime, fingerprint,
EXIF, sniffed content kind. `seam.build_review_set` is the enforcement point -- whatever
the engine knows about a file lands on its review item, and `_render_item` shows all of
it to every critic. The failure this prevents was measured on the real library: filename
regex alone mis-bucketed accounting chapters as CS (`chap\d\d`) and an onboarding deck
as quizzing (`final`), while `LVC.pptx` (really "SPORTS QUIZ- PRELIM") was invisible
until its embedded title was read.

## 3. The reliability protocol

**Schema-validated outputs with bounded repair.** Parse → validate against the task
schema → on failure, exactly one repair attempt (the validator's error is shown to the
model) → on second failure, escalate to the next chain entry → if the chain is
exhausted, the item routes to REVIEW/human. Malformed output can never enter a plan.

**Abstention is a first-class answer.** Every task schema includes `UNSURE` plus a
confidence field. UNSURE routes up the escalation ladder (local → stronger model →
human) and is *scored as a good outcome* in evals when the item is genuinely ambiguous.
This mirrors the engine's no-implicit-'Other' philosophy (defect L4): a small model that
knows its limits beats a large model that guesses.

**Self-consistency where tokens are free.** Below a confidence threshold, local
classifications are re-sampled N=3 and majority-voted. On your own GPU the marginal cost
is watts, and it is precisely where a 20B buys back accuracy.

**Deterministic post-verification.** Agent decisions pass through the same coverage and
threshold machinery as human-authored rules; orchestrator actions must match a
`suggested_next` entry byte-for-byte; and everything the agent proposes becomes a *plan*
— which means per-row precondition re-verification, journaling, and human-gated
execution, same as any other plan.

**Capability tiers.** Each task's spec (in code, on `TaskSpec`) carries
`tier = "any"` or `"strong"`. The chain serves `any`-tier calls from every entry;
for `strong`-tier calls it skips the `local` slot — the generalization of "text stays
local, hard judgment goes to a stronger model." Escalation after repeated schema
failures re-runs the task at `strong`, so a struggling local model hands off rather
than guessing. v0.1's shipped tasks are all `any` (local-eligible); per-task tier
overrides in config are deliberately not a surface yet.

## 4. The tasks

| Task | Input (pre-digested) | Output (schema-bound) |
|---|---|---|
| `mlo agent classify` | UNMATCHED tail from the coverage machinery: filename/path tokens + exemplars per cluster | `{label ∈ config taxonomy, confidence, UNSURE?}` per item |
| `mlo agent classify --media` (v0.2) | the router's unrouted media list (`--paths`, the sidecar `plan reorganize` writes) — a deterministic name-pattern pre-pass (docs/classification-patterns.md) settles the definitional ~90% with no LLM call; only the tail reaches the chain | per item: `media_kind ∈ [movie, tv, personal, music, junk]`, `language ∈` configured names + default, `year ∈ 1900–2035 ∣ null`, confidence — hints JSON for `plan --hints`; `junk` never hints (stays put) and lands in a `*-junk.json` sidecar for triage |
| `mlo agent triage` | A REVIEW pile's rollups (ext × folder × bytes) + sampled exemplars | per-cluster `{disposition ∈ [keep-organize, stage-junk, needs-human], rationale, confidence}` |
| `mlo agent run` | latest `summary.json` | pick one of `suggested_next` (or `stop`), with rationale — loop continues through the engine's own gates |
| `mlo agent eval` | golden sets in `evals/` | accuracy / abstention / escalation metrics per chain configuration |

`triage` is the marquee case because it replaces the exact judgment a human exercised in
the real consolidation this project distills: deciding that a 100K-file `.txt/.html`
scrape is junk, that 24 `.vob` files inside it are DVD rips worth keeping, and that nine
`.crypt8` files are WhatsApp backups someone will cry about losing.

`classify --media` follows the same containment rules as everything else: every option
space is enumerated (kinds, configured languages, a bounded year range — booleans are
rejected as years), `media_kind = UNSURE` drops the whole item to the unsure list (kind
is load-bearing), `language = UNSURE` merely leaves language to the router's token
detection and explicit default. The output is *identity*, never *placement*: hints feed
`taxonomy.route()`, whose result still becomes an ordinary gated, rehearsed plan.

## 5. Evals — "high quality" is measured, not claimed

`evals/` contains golden labeled sets distilled (and anonymized) from the real
consolidation's decision history: language/taxonomy classification items,
movie-vs-TV-vs-home-video calls, junk-vs-personal triage clusters. Targets:

- **accuracy** on decided items,
- **abstention rate** (UNSURE on genuinely ambiguous items is correct behavior),
- **dangerous-error rate** — the metric that matters: *personal-content clusters
  dispositioned as junk*. The eval harness weights this asymmetrically; a chain
  configuration that is 95% accurate but ever throws away baby videos loses to one that
  is 85% accurate and always abstains on them. A live `mlo agent eval` that counts any
  dangerous error exits 3.
- the deterministic-guard rate (clusters the post-check downgraded to needs-human).
  Per-configuration latency and fallback-hop tracking is now recorded in each eval run's
  `agent-ledger.jsonl` + `summary.json` `chain_ledger` rollup; token-priced cost is left to
  the reader (no hardcoded price table lives in the engine — L6).

CI runs the harness against a rule-based mock endpoint (no live model needed for the
protocol's own regression tests); live results per model land in the table below as they
are measured.

### Results

Measured 2026-07-06 on the v0.1 golden sets (`evals/classify.json`, 48 items;
`evals/triage.json`, 16 clusters) with `gpt-oss:20b` running locally on Ollama. The model
is stochastic (temperature 0.2), so several runs are shown to convey the spread; the number
that matters — **dangerous errors, i.e. personal content dispositioned as junk — was 0 in
every run.** The latency column is the per-call average the run's `agent-ledger.jsonl`
now records — the concrete cost of local reasoning at this size.

| Chain configuration | classify accuracy (decided) | classify abstention | triage accuracy (decided) | **dangerous errors** | avg latency/call |
|---|---|---|---|---|---|
| `gpt-oss:20b` (local only), run A | 1.00 (31 decided) | 6% (9/15 ambiguous abstained) | 0.80 (15 decided) | **0** | — |
| `gpt-oss:20b` (local only), run B | 0.97 (32 decided) | 3% (8/15 ambiguous abstained) | 0.71 (14 decided) | **0** | — |
| `gpt-oss:20b` (local only), run C | 1.00 (32 decided) | 3% (1/15 ambiguous abstained) | 0.87 (15 decided) | **0** | 56.7 s |
| local → cloud escalation | *runnable — see below* | | | | |
| cloud only (reference) | *runnable — see below* | | | | |

The last two rows measure themselves once cloud keys are set — no code work remains, only
the run:

```console
$ export MLO_ANTHROPIC_KEY=...          # (or MLO_GEMINI_KEY / MLO_OPENAI_KEY)
$ mlo agent eval --chain local,claude-haiku-4-5   # local → cloud escalation row
$ mlo agent eval --chain claude-haiku-4-5         # cloud-only reference row
```

Each writes its metrics + a `chain_ledger` rollup (calls answered, entry mix, fallback
hops, average latency) to the run's `summary.json`, and the full per-call ledger to
`agent-ledger.jsonl`.

Reading it: on the items it chose to decide, the local 20B classified files ~97–100%
correctly and abstained on the genuinely-ambiguous ones; on triage it correctly kept
personal media and staged junk ~71–87% of the time, routing the rest to `needs-human`
rather than guessing. Across 44 triage-cluster decisions over three runs it never once sent
personal content toward disposal — which is the whole point: the protocol makes a small
local model's *mistakes* land on the safe side of the line the kernel already can't cross.
The flip side the ledger now makes visible: ~57 s per call at this model size — accuracy
this shape is not free, which is exactly why the chain lets you position `local` first for
privacy or last for speed. Reproduce with `mlo agent eval` (or `--mock` for the model-free
harness check).

## 6. What the agent layer is not

- It is not autonomous file management. Every mutation it proposes becomes a dry-run
  plan a human (or an explicitly configured gate) applies.
- It is not a chat interface. There is no free-form "do what I mean" entry point; there
  are tasks with schemas.
- It is not required. With `[llm] enabled = false`, agent commands refuse cleanly
  (exit 2) and everything they'd assist with still exists deterministically: coverage
  reports name unmatched tokens, REVIEW piles export as CSVs, rules live in config.
