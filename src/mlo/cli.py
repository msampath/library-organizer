"""The mlo command line. Exit codes are API (docs/formats.md):

  0 ok · 1 unexpected error · 2 config/validation · 3 completed with
  drift/residuals or blocking findings · 4 stale input refused · 5 coverage
  threshold blocked.

Every refusal prints its own remedy — the gates name the command that fixes
them (defect L7/L8 philosophy: the system carries the discipline).
"""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__, apply as applymod, hints as hintsmod
from . import pilot as pilotmod
from . import plan as planmod, report, scan, undo as undomod, verdict
from .agent.llm import LLMDisabled
from .config import Config, ConfigError, load, validate
from .store import Store
from .verdict import StaleArtifactError


def _workspace(cfg_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(cfg_path)), ".mlo")


def _open(args) -> tuple[Config, Store]:
    # P21/B8: an optional workspace .env, loaded before anything that might
    # read MLO_*_KEY env vars (agent chain adapters, enrich connectors).
    # Precedence: an already-set env var always wins — .env only fills gaps.
    from .dotenv import load_dotenv
    load_dotenv(os.path.join(_workspace(args.config), ".env"))
    cfg = load(args.config)
    notes = validate(cfg, _workspace(args.config))
    for n in notes:
        print(f"note: {n}")
    return cfg, Store.open(_workspace(args.config))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="mlo",
        description="safety-first media/personal-data consolidation",
        # No prefix abbreviation: `agent run --act` re-enters main() with a
        # forwarded argv and refuses --config re-pointing — an abbreviated
        # `--conf` must not slip past that guard (or surprise any script).
        allow_abbrev=False)
    p.add_argument("--config", default="mlo.toml", help="path to mlo.toml")
    p.add_argument("--version", action="version", version=f"mlo {__version__}")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="P21/C4: per-file diagnostic chatter to stderr (hint "
                        "augmenters, crash-reconcile detail) and the full "
                        "unreadable-file list on scan, instead of a 10-line "
                        "sample — no log files, the SQLite journal is the "
                        "record of what the system did")
    sub = p.add_subparsers(dest="cmd", required=True)

    ip = sub.add_parser("init", help="write an annotated starter mlo.toml")
    ip.add_argument("--interview", action="store_true",
                    help="ask the config-surface questions and generate mlo.toml")
    sp = sub.add_parser("serve", help="open the guided web UI (localhost only)")
    sp.add_argument("--port", type=int, default=8765)
    sub.add_parser("check", help="validate config + reachability + store health")
    sub.add_parser("status", help="artifacts, journal, and what to do next")
    sub.add_parser("doctor", help="P21/C5: version, config, root/staging "
                   "reachability + writability, store health, LLM chain "
                   "preflight, last run — one command a support flow can ask for")

    sp = sub.add_parser("scan", help="fingerprint the library or a source")
    sp.add_argument("target", help="'library' or a source name")
    sp.add_argument("--rehash-under", action="append", default=[],
                    help="(library) force re-hash of files under this prefix, "
                         "bypassing the size+mtime fast-path — refreshes a "
                         "silently-changed file (bit-rot / torn read); repeatable")

    sp = sub.add_parser("verdicts", help="classify a source against the library")
    sp.add_argument("source")

    sp = sub.add_parser("sweep", help="consolidate source(s) into the library and "
                                      "stage the already-present originals out")
    sp.add_argument("sources", nargs="*",
                    help="source names to sweep (default: every enabled source)")
    sp.add_argument("--confirm-mb", type=int, default=1,
                    help="re-confirm each ORGANIZED file against its library twin "
                         "before staging it out (0 disables; any nonzero value "
                         "enables the fixed quick+full-hash confirmation policy, "
                         "P21/A3); default 1 (on), matching `mlo pilot`")
    sp.add_argument("--waive-organize", action="store_true",
                    help="stage a source's duplicates even if it still has UNIQUE "
                         "files (they are left in place, not staged) — for uniques "
                         "that are a human-review pile, not library candidates")
    sp.add_argument("--execute", action="store_true",
                    help="apply the staging (default: rehearse and report only)")

    sp = sub.add_parser("plan", help="build a plan artifact")
    sp.add_argument("kind", choices=["dedup", "organize", "reorganize",
                                     "dedup-library", "stage-library",
                                     "prune-empty", "date-drain", "relocate",
                                     "flatten-provenance", "containers",
                                     "bad-archives"])
    sp.add_argument("source", nargs="?",
                    help="source name (dedup/organize); omitted for the "
                         "library-side kinds")
    sp.add_argument("--waive-organize", action="store_true",
                    help="stage even though UNIQUE files are unorganized (L13)")
    sp.add_argument("--confirm-mb", type=int, default=1,
                    help="(dedup) re-confirm each ORGANIZED file against its "
                         "library twin before staging it out (0 disables; any "
                         "nonzero value enables the fixed quick+full-hash "
                         "confirmation policy, P21/A3); a mismatch keeps the file "
                         "in place (destructive-sweep bar); default 1 (on)")
    sp.add_argument("--under", action="append", default=[],
                    help="(reorganize/dedup-library) only examine library paths "
                         "under this prefix; repeatable — everything else is "
                         "untouchable")
    sp.add_argument("--hints", default=None,
                    help="JSON file of agent/EXIF hints: "
                         '{relpath: {media_kind, language, year}}')
    sp.add_argument("--exif", action="store_true",
                    help="read EXIF years for in-scope photos (organize/reorganize)")
    sp.add_argument("--sniff", action="store_true",
                    help="(reorganize) content-sniff in-scope files with no "
                         "taxonomy bucket: route false-carves by magic bytes "
                         "into their media type's Unclassified holding pen")
    sp.add_argument("--sniff-min-mb", type=float, default=0.0,
                    help="(reorganize --sniff) only sniff files at least N MiB — "
                         "focuses on media-sized carves and avoids weak-signature "
                         "false positives on tiny files (default 0 = all)")
    sp.add_argument("--paths", default=None,
                    help="(stage-library) JSON list of library relpaths to stage")
    sp.add_argument("--map", default=None,
                    help="(relocate) JSON object of relpath -> dest_relpath — "
                         "an approved critic/human placement mapping")
    sp.add_argument("--label", default="triage",
                    help="(stage-library) staging subfolder (default: triage)")

    sp = sub.add_parser("apply", help="rehearse (default) or execute a plan")
    sp.add_argument("plan_path")
    sp.add_argument("--execute", action="store_true")
    sp.add_argument("--confirm-dispose", type=int, default=None,
                    help="required with --execute on a 'dispose' plan: the "
                         "exact row count shown when it was built — typed "
                         "confirmation so disposal is never armed by habit")

    sp = sub.add_parser("dispose", help="P21/C2: build a plan to send every "
                        "journal-explained file in staging to the OS "
                        "recycle bin / trash; apply it like any other plan, "
                        "with --confirm-dispose")
    sp.add_argument("--staging", default=None,
                    help="dispose only this staging key's root (default: "
                         "every configured staging root)")

    sp = sub.add_parser("undo", help="P21/C1: build a reverse plan for a "
                        "run's placement ops (dst -> src); apply it like "
                        "any other plan")
    sp.add_argument("run_id")

    sp = sub.add_parser("pilot", help="Pass 1: analyze everything read-only "
                        "into one sealed proposal; Pass 2 (--execute) runs "
                        "the approved sections")
    sp.add_argument("sources", nargs="*",
                    help="source names to analyze (default: every enabled)")
    sp.add_argument("--under", action="append", default=[],
                    help="library scope prefix for the library-side builders; "
                         "repeatable (default: whole library, C19 in full)")
    sp.add_argument("--confirm-mb", type=int, default=1,
                    help="dedup twin re-confirm bar in MiB (default 1)")
    sp.add_argument("--chain",
                    help="critic chain override, e.g. claude-opus-4-8,local "
                         "(resolution: --chain > [llm] critics_chain > chain)")
    sp.add_argument("--critic-limit", type=int, default=500,
                    help="max review items sent to critics; overflow joins the "
                         "human queue (default 500)")
    sp.add_argument("--cross-check", action="store_true",
                    help="second critic + adversarial tiebreak (token-costly)")
    sp.add_argument("--live-search", action="store_true",
                    help="actually run the critic's composed web-search query "
                         "against [enrich] searxng_url (P21/B2) — without this "
                         "flag, evidence is composed but never searched")
    sp.add_argument("--hints", default=None,
                    help="reuse a prior run's hints JSON (resumability)")
    sp.add_argument("--no-exif", action="store_true",
                    help="skip EXIF year reading")
    sp.add_argument("--sniff-min-mb", type=float, default=None,
                    help="content-sniff unbucketed files at least N MiB")
    sp.add_argument("--execute", action="store_true",
                    help="Pass 2: execute the approved sections of a proposal "
                         "with bounded convergence, then verify")
    sp.add_argument("--proposal", default=None,
                    help="(--execute) proposal.json path")
    sp.add_argument("--approvals", default=None,
                    help="(--execute) approvals JSON (mlo.approvals/1), bound "
                         "to the proposal by its hash")
    sp.add_argument("--approve-all", action="store_true",
                    help="(--execute) approve every ready/gated section; the "
                         "synthesized approvals are persisted for audit")

    sp = sub.add_parser("identify", help="P21/B6: the productized identification "
                        "loop — slice a review-set into batches, run the critic "
                        "chain, merge into one schema-validated hints file")
    sp.add_argument("--source", default=None,
                    help="build a review-set from this source's REVIEW pile "
                         "(content-sniffed) and identify over it")
    sp.add_argument("--review-set", default=None,
                    help="identify over an existing review-set.jsonl instead "
                         "of building one from a source")
    sp.add_argument("--batch-size", type=int, default=500,
                    help="items per critic batch (default 500)")
    sp.add_argument("--chain",
                    help="critic chain override, e.g. claude-opus-4-8,local")
    sp.add_argument("--cross-check", action="store_true",
                    help="second critic + adversarial tiebreak (token-costly)")
    sp.add_argument("--live-search", action="store_true",
                    help="run composed web-search queries against "
                         "[enrich] searxng_url (P21/B2)")
    sp.add_argument("--prior-hints", default=None,
                    help="seed the merge from a prior identify/critics hints "
                         "JSON (incremental resumption)")
    sp.add_argument("--out", default="identify-hints.json",
                    help="artifact name (stem) for the merged hints JSON, "
                         "written into the run directory")

    sp = sub.add_parser("snapshot", help="inventory the library's current state "
                        "(problem folders + suspected homes) for the eval loop")
    sp.add_argument("--under", default=None,
                    help="only inventory library paths under this prefix")

    sp = sub.add_parser("verify", help="check library/staging against the store")
    sp.add_argument("what", choices=["library", "staging"])
    sp.add_argument("--deep", action="store_true",
                    help="re-fingerprint every file (default: fast stat-diff)")

    sp = sub.add_parser("export", help="CSV view of a store table")
    sp.add_argument("table", choices=["ops", "files", "source"])
    sp.add_argument("name", nargs="?", help="source name (table=source)")

    sp = sub.add_parser("agent", help="LLM-assisted tasks (opt-in via [llm])")
    asub = sp.add_subparsers(dest="agent_cmd", required=True)
    a = asub.add_parser("classify", help="propose labels for the REVIEW tail, "
                        "or (--media) media identities for the router")
    a.add_argument("source", nargs="?")
    a.add_argument("--limit", type=int, default=None)
    a.add_argument("--media", action="store_true",
                   help="classify media kind/language/year for router hints")
    a.add_argument("--paths", default=None,
                   help="(--media) JSON list of relpaths (e.g. the unrouted "
                        "sidecar a reorganize plan wrote)")
    a.add_argument("--out", default="hints.json",
                   help="(--media) artifact name (stem) for the hints JSON, "
                        "written into the run directory")
    a = asub.add_parser("critics", help="run the specialist critic panel over a "
                        "review-set -> router hints")
    a.add_argument("--review-set", default=None,
                   help="review-set.jsonl emitted by `plan reorganize`")
    a.add_argument("--source", default=None,
                   help="build a review-set from this source's REVIEW pile "
                        "(content-sniffed) and run the panel over it")
    a.add_argument("--out", default="critic-hints.json",
                   help="artifact name (stem) for the hints JSON, "
                        "written into the run directory")
    a.add_argument("--live-search", action="store_true",
                   help="actually run each item's composed web-search query "
                        "against [enrich] searxng_url (P21/B2) — without this "
                        "flag, the panel runs with no evidence at all")
    a.add_argument("--cross-check", action="store_true",
                   help="run a second critic on ambiguous video and adversarially "
                        "tiebreak disagreements")
    a.add_argument("--limit", type=int, default=None,
                   help="run the panel on only the first N review items (a "
                        "bounded, labeled sample of a large pile)")
    a.add_argument("--chain",
                   help="run the critics on a specific chain instead of the "
                        "configured one, e.g. --chain claude-opus-4-8,local. "
                        "Resolution: --chain > [llm] critics_chain > [llm] chain; "
                        "the [llm] enabled kill-switch is never overridden")

    a = asub.add_parser("triage", help="recommend dispositions for a REVIEW pile")
    a.add_argument("source")
    a = asub.add_parser("improve", help="self-improving loop over dogfood "
                        "fixtures — dry-run, distils rules, never touches files")
    a.add_argument("--dogfood", default="evals/dogfood",
                   help="directory of labeled fixture JSON lists")
    a.add_argument("--known", default="evals/known-failures.jsonl",
                   help="past-failure corpus (the regression guard)")

    a = asub.add_parser("run", help="pick (and with --act, run) the next command")
    a.add_argument("--steps", type=int, default=1)
    a.add_argument("--act", action="store_true",
                   help="actually dispatch the chosen commands")
    a = asub.add_parser("eval", help="measure the chain on the golden sets")
    a.add_argument("--mock", action="store_true",
                   help="deterministic heuristic endpoint (CI harness check)")
    a.add_argument("--dir", default="evals", help="golden-set directory")
    a.add_argument("--chain",
                   help="measure a specific chain instead of the configured one, "
                        "e.g. --chain local,claude-haiku-4-5 (escalation row) or "
                        "--chain claude-haiku-4-5 (cloud-only). The local slot is "
                        "active only when 'local' is in the chain.")

    args = p.parse_args(argv)

    try:
        return _dispatch(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except LLMDisabled as e:
        print(f"agent disabled: {e}", file=sys.stderr)
        return 2
    except (planmod.PlanError, planmod.OrderingError,
            report.PlanIntegrityError, applymod.DisposeNotConfirmed) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    except StaleArtifactError as e:
        print(f"stale input refused: {e}", file=sys.stderr)
        return 4
    except pilotmod.ApprovalsError as e:
        print(f"approvals refused: {e}", file=sys.stderr)
        return 4
    except planmod.CoverageBlockedError as e:
        print(f"coverage blocked: {e}", file=sys.stderr)
        return 5
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 1


def _dispatch(args) -> int:
    if args.cmd == "init":
        try:
            if args.interview:
                from . import interview
                report.write_config(
                    args.config, interview.build_config_toml(
                        interview.run_interview()))
            else:
                report.write_starter_config(args.config)
        except FileExistsError:
            print(f"refusing to overwrite existing {args.config}", file=sys.stderr)
            return 2
        print(f"wrote {args.config} — review it, then run: mlo check")
        return 0

    if args.cmd == "serve":
        from . import web
        return web.serve(args.config, args.port)

    if args.cmd == "doctor":
        # doctor exists to diagnose broken setups — a hard validate refusal
        # (unreachable library root, dead source drive) must not prevent the
        # diagnosis (super-review B-004). Config must still PARSE; the
        # reachability findings validate() would refuse on are exactly what
        # the report renders as [MISSING].
        from .dotenv import load_dotenv
        load_dotenv(os.path.join(_workspace(args.config), ".env"))
        cfg = load(args.config)
        try:
            for n in validate(cfg, _workspace(args.config)):
                print(f"note: {n}")
        except ConfigError as e:
            print(f"note: config validation failed ({e}) — diagnosing anyway")
        store = Store.open(_workspace(args.config))
        try:
            return _run_command(args, cfg, store)
        finally:
            store.close()

    cfg, store = _open(args)
    try:
        return _run_command(args, cfg, store)
    finally:
        store.close()


def _warn_pending(store: Store) -> None:
    """P21/C6: surfaced crash recovery — `check`/`status`/`doctor` all detect
    leftover pending journal rows and name the remedy, instead of a crash's
    only visible trace being a silent reconcile buried inside the next
    `apply --execute`."""
    n = len(store.pending_ops())
    if n:
        print(f"  WARNING: {n} pending journal row(s) — a prior run may have "
              f"crashed; the next `mlo apply ... --execute` reconciles them "
              f"automatically (-v shows which)")


def _warn_live_search_unconfigured(args, cfg: Config) -> None:
    """--live-search without [enrich] searxng_url silently degrades to the
    offline path (queries composed, never searched — the C55 ghost); an
    explicit flag deserves an explicit warning (super-review B-008)."""
    if getattr(args, "live_search", False) and not cfg.enrich.searxng_url:
        print("warning: --live-search is set but [enrich] searxng_url is not "
              "configured — evidence queries will be composed but never "
              "searched", file=sys.stderr)


def _run_command(args, cfg: Config, store: Store) -> int:
    ws = store.workspace

    if args.cmd == "check":
        print(f"config ok: {cfg.path} (hash {cfg.config_hash[:12]})")
        print(f"library: {cfg.library_root}")
        for s in cfg.sources:
            state = "enabled" if s.enabled else "DISABLED"
            print(f"source {s.name}: {s.root} [{state}]")
        print(f"store: {os.path.join(ws, 'state.db')} "
              f"(journal at {store.journal_pos()})")
        _warn_pending(store)
        # P21/B8: `check`'s help has always promised "reachability" — the LLM
        # chain was the one reachability check it never actually ran
        # (closes half of G536). Informational only: a broken/unreachable
        # entry never fails `mlo check` — the agent layer is opt-in.
        if cfg.llm.enabled and cfg.llm.chain:
            from .agent.llm import preflight
            print("llm chain:")
            for r in preflight(cfg):
                print(f"  {r.entry:<20s} {'ok' if r.ok else 'UNREACHABLE'}  "
                      f"{r.detail}")
        return 0

    if args.cmd == "status":
        print(f"journal position: {store.journal_pos()}")
        _warn_pending(store)
        arts = store.artifacts_all()
        if not arts:
            print("no artifacts yet — start with: mlo scan library")
            return 0
        for a in arts:
            print(f"  {a.artifact_id:<40s} {a.status:<9s} "
                  f"pos={a.journal_pos} built={a.built_at}")
        for a in arts:
            if a.status not in ("stale", "building"):
                continue
            name = a.scope.get("name")            # scan artifacts carry the name
            kind_cmd = {"scan": f"mlo scan {name}" if name else "",
                        "verdicts": f"mlo verdicts {a.artifact_id.split(':', 1)[-1]}",
                        "index": "mlo scan library"}.get(a.kind, "")
            print(f"next: refresh {a.artifact_id}"
                  + (f" — {kind_cmd}" if kind_cmd else ""))
        return 0

    if args.cmd == "doctor":
        from . import doctor as doctormod
        rep = doctormod.report(cfg, store)
        print(f"mlo {rep['version']}")
        print(f"config: {rep['config_path']} (hash {rep['config_hash'][:12]})")
        print(f"library: {rep['library']['root']} [{rep['library']['status']}]")
        for s in rep["sources"]:
            print(f"source {s['name']}: {s['root']} [{s['status']}]")
        for st in rep["staging"]:
            print(f"staging {st['key']}: {st['root']} [{st['status']}]")
        print(f"store: {rep['store']['path']} "
              f"(journal at {rep['store']['journal_pos']})")
        _warn_pending(store)
        if rep["store"]["stale_artifacts"]:
            print(f"  stale artifacts: {', '.join(rep['store']['stale_artifacts'])}")
        if rep["llm_chain"] is not None:
            print("llm chain:")
            for e in rep["llm_chain"]:
                print(f"  {e['entry']:<20s} {'ok' if e['ok'] else 'UNREACHABLE'}  "
                      f"{e['detail']}")
        if rep["last_run"]:
            lr = rep["last_run"]
            print(f"last run: {lr['command']} ({lr['status']}) "
                  f"started {lr['started_at']}")
        else:
            print("last run: none yet")
        return 0

    run_id = store.start_run(args.cmd, sys.argv[1:], cfg.config_hash, __version__)
    status = "completed"
    try:
        code = _run_stateful(args, cfg, store, run_id)
        if code == 3:
            status = "completed_with_residuals"
        elif code not in (0,):
            status = "failed"
        return code
    except BaseException:
        status = "failed"
        raise
    finally:
        store.finish_run(run_id, status)


def _run_stateful(args, cfg: Config, store: Store, run_id: str) -> int:
    if args.cmd == "scan":
        if args.target == "library":
            n, skipped = scan.scan_library(store, cfg, run_id,
                                           rehash_under=args.rehash_under)
            print(f"library indexed: {store.index_count()} files "
                  f"({n} hashed this run, {len(skipped)} unreadable)")
        else:
            n, skipped = scan.scan_source(store, cfg, args.target, run_id)
            print(f"source '{args.target}' scanned: {n} files "
                  f"({len(skipped)} unreadable)")
        if args.verbose:
            for s in skipped:
                print(f"  unreadable: {s}", file=sys.stderr)
        else:
            for s in skipped[:10]:
                print(f"  unreadable: {s}")
            if len(skipped) > 10:
                print(f"  ... and {len(skipped) - 10} more (-v for the full list)")
        return 0

    if args.cmd == "sweep":
        return _run_sweep(args, cfg, store, run_id)

    if args.cmd == "verdicts":
        counts = verdict.assign(store, cfg, args.source, run_id)
        total = sum(counts.values())
        print(f"verdicts for '{args.source}' ({total} files): "
              + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        if counts.get("REVIEW"):
            print(f"next: mlo plan organize {args.source}  "
                  f"(REVIEW pile stays put until you decide)")
        return 0

    if args.cmd == "plan":
        if args.kind in ("dedup", "organize") and not args.source:
            print(f"plan {args.kind} needs a source name", file=sys.stderr)
            return 2
        hints = hintsmod.load_hints(args.hints)
        if args.kind == "organize":
            if args.exif:
                hints = hintsmod.augment_exif_source(cfg, store, args.source, hints,
                                                     verbose=args.verbose)
            res = planmod.build_organize(store, cfg, args.source, hints=hints)
        elif args.kind == "reorganize":
            if args.exif:
                hints = hintsmod.augment_exif_library(cfg, store, args.under, hints,
                                                      verbose=args.verbose)
            if args.sniff:
                hints = hintsmod.augment_sniff_library(cfg, store, args.under, hints,
                                                     args.sniff_min_mb,
                                                     verbose=args.verbose)
            if hintsmod.book_exts(cfg):
                hints = hintsmod.augment_bookmeta_library(cfg, store, args.under,
                                                          hints, verbose=args.verbose)
            res = planmod.build_reorganize(store, cfg, under=args.under,
                                           hints=hints)
        elif args.kind == "dedup-library":
            res = planmod.build_dedup_library(store, cfg, under=args.under)
        elif args.kind == "prune-empty":
            res = planmod.build_prune_empty(store, cfg, under=args.under)
        elif args.kind == "date-drain":
            res = planmod.build_date_drain(store, cfg, under=args.under)
        elif args.kind == "flatten-provenance":
            res = planmod.build_flatten_provenance(store, cfg, under=args.under)
        elif args.kind == "containers":
            res = planmod.build_containers(store, cfg, under=args.under)
        elif args.kind == "bad-archives":
            res = planmod.build_bad_archives(store, cfg, under=args.under)
        elif args.kind == "relocate":
            import json as jsonmod2
            from .config import ConfigError
            if not args.map:
                print("plan relocate needs --map <json of relpath: dest_relpath>",
                      file=sys.stderr)
                return 2
            try:
                with open(args.map, encoding="utf-8") as f:
                    relmap = jsonmod2.load(f)
            except (OSError, jsonmod2.JSONDecodeError) as e:
                raise ConfigError(f"cannot read relocate map {args.map}: {e}")
            if not isinstance(relmap, dict):
                raise ConfigError(f"{args.map} must be a JSON object of "
                                  f"relpath -> dest_relpath")
            res = planmod.build_relocate(store, cfg, relmap)
        elif args.kind == "stage-library":
            import json as jsonmod
            from .config import ConfigError
            if not args.paths:
                print("plan stage-library needs --paths <json list of relpaths>",
                      file=sys.stderr)
                return 2
            try:
                with open(args.paths, encoding="utf-8") as f:
                    stage_paths = jsonmod.load(f)
            except (OSError, jsonmod.JSONDecodeError) as e:
                raise ConfigError(f"cannot read paths file {args.paths}: {e}")
            if not isinstance(stage_paths, list):
                raise ConfigError(f"{args.paths} must be a JSON list of relpaths")
            res = planmod.build_stage_library(store, cfg, stage_paths,
                                              label=args.label)
        else:
            res = planmod.build_dedup(store, cfg, args.source,
                                      waive_organize=args.waive_organize,
                                      confirm_bytes=args.confirm_mb * 1024 * 1024)
        print(f"plan {res.plan_id[:12]} ({res.kind}, {res.n_rows} ops): {res.path}")
        for n in res.notes:
            print(f"  note: {n}")
        if res.unrouted:
            upath = report.write_json(store.workspace, run_id, "unrouted",
                                      res.unrouted)
            # The engine->agents seam (§3.3): enrich the unrouted residue into a
            # self-contained review-set the critics judge — no 'go read 5
            # files'. CANONICAL: every item carries ALL signals a human would
            # read (path, siblings, embedded doc props, dates), never a bare
            # filename.
            from . import provenance, seam
            idx = {r["relpath"]: r for r in store.index_iter()}
            rows = [idx[rel] for rel in res.unrouted if rel in idx]
            items = seam.build_review_set(
                cfg, rows, origin_map=provenance.build_origin_map(store),
                sibling_index=seam.build_sibling_index(idx.keys()),
                doc_props=hintsmod.doc_props_map(cfg.library_root, rows))
            rpath = report.write_review_set(store.workspace, run_id, items)
            print(f"  {len(res.unrouted)} media files had no derivable identity "
                  f"(they stay put): {upath}")
            print(f"  review-set for the critics: {rpath}")
            print(f"  next: mlo agent classify --media --paths \"{upath}\"   "
                  f"then re-plan with --hints <printed hints path>")
        print(f"next: mlo apply \"{res.path}\"          (rehearse)")
        print(f"      mlo apply \"{res.path}\" --execute")
        return 0

    if args.cmd == "undo":
        res = undomod.build_undo(store, cfg, args.run_id)
        print(f"plan {res.plan_id[:12]} ({res.kind}, {res.n_rows} ops): {res.path}")
        for n in res.notes:
            print(f"  note: {n}")
        print(f"next: mlo apply \"{res.path}\"          (rehearse)")
        print(f"      mlo apply \"{res.path}\" --execute")
        return 0

    if args.cmd == "dispose":
        res = planmod.build_dispose(store, cfg, staging_key=args.staging)
        print(f"plan {res.plan_id[:12]} ({res.kind}, {res.n_rows} ops): {res.path}")
        for n in res.notes:
            print(f"  note: {n}")
        print(f"next: mlo apply \"{res.path}\"          (rehearse)")
        print(f"      mlo apply \"{res.path}\" --execute "
              f"--confirm-dispose {res.n_rows}")
        return 0

    if args.cmd == "apply":
        if args.execute:
            store.snapshot()
        res = applymod.apply_plan(store, cfg, args.plan_path, run_id,
                                  execute=args.execute, verbose=args.verbose,
                                  confirm_dispose=args.confirm_dispose)
        mode = "EXECUTE" if args.execute else "REHEARSAL"
        print(f"{mode} {res.plan_id[:12]}: "
              + "  ".join(f"{k}={v}" for k, v in sorted(res.counts.items())))
        for w in res.warnings:
            print(f"  warning: {w}")
        for d in res.drift[:10]:
            print(f"  drift: {d['src']} — {d['detail']}")
        for a in res.audit_failures[:10]:
            print(f"  audit: {a['dst']} — {a['detail']}")
        if res.residual_plan:
            print(f"  residual plan: {res.residual_plan}")
        print(f"  summary: {res.summary_path}")
        return res.exit_code

    if args.cmd == "pilot" and args.execute:
        from .config import ConfigError
        if not args.proposal:
            print("pilot --execute needs --proposal <proposal.json>",
                  file=sys.stderr)
            return 2
        if bool(args.approvals) == bool(args.approve_all):
            print("pilot --execute needs exactly one of --approvals <file> "
                  "or --approve-all", file=sys.stderr)
            return 2
        if args.approve_all:
            proposal = report.read_proposal(args.proposal)
            approvals = pilotmod.approve_all(proposal)
            apath = report.write_json(store.workspace, run_id,
                                      "approvals", approvals)
            print(f"approve-all: {len(approvals['decisions'])} section(s) "
                  f"approved (audit: {apath})")
        else:
            approvals = pilotmod.load_approvals(args.approvals)
        res = pilotmod.execute(
            store, cfg, run_id, args.proposal, approvals,
            progress=lambda phase, info: print(
                f"  [{phase}] " + " ".join(f"{k}={v}" for k, v in info.items())))
        for o in res.outcomes:
            line = (f"  {o.id:<28s} {o.status:<13s} cycles={o.cycles} "
                    f"drift={o.drift}")
            if o.unconverged_rows:
                line += f" unconverged={o.unconverged_rows}"
            if o.rejected_dropped:
                line += f" rejected_dropped={o.rejected_dropped}"
            print(line)
        v = res.verify
        print(f"  verify: library {v['library']}  staging {v['staging']}")
        print(f"  summary: {res.summary_path}")
        if res.staging:
            print("  staged for disposal (recoverable; mlo moves to the OS "
                  "recycle bin/trash only via `mlo dispose`, never deletes): "
                  + "  ".join(f"{k}={s['staged']}" for k, s in
                              res.staging.items()))
        return res.exit_code

    if args.cmd == "pilot":
        _warn_live_search_unconfigured(args, cfg)
        chain = tuple(c.strip() for c in args.chain.split(",") if c.strip()) \
            if args.chain else None
        res = pilotmod.analyze(
            store, cfg, run_id,
            sources=args.sources or None,
            under=args.under,
            confirm_bytes=args.confirm_mb * 1024 * 1024,
            chain=chain,
            critic_limit=args.critic_limit,
            cross_check=args.cross_check,
            hints_path=args.hints,
            exif=not args.no_exif,
            sniff_min_mb=args.sniff_min_mb,
            live_search=args.live_search,
            verbose=args.verbose,
            progress=lambda phase, info: print(
                f"  [{phase}] " + " ".join(f"{k}={v}" for k, v in info.items())))
        print(f"proposal: {res.proposal_path}")
        for s in res.sections:
            line = f"  {s.id:<28s} {s.status:<8s} rows={s.n_rows}"
            if s.rehearsal:
                line += (f" would_do={s.rehearsal['would_do']}"
                         f" drift={s.rehearsal['drift']}")
            if s.blocked_reason:
                line += "  BLOCKED"
            print(line)
        rv = res.review
        print(f"  review queue: {rv['hinted']} critic-hinted, "
              f"{len(rv['unsure_relpaths'])} for a human")
        print("next: mlo serve   (review the proposal in the UI)")
        print(f"      mlo pilot --execute --proposal \"{res.proposal_path}\" "
              f"--approvals <file-from-review>")
        return res.exit_code

    if args.cmd == "identify":
        from . import identify as identifymod
        from . import seam, sniff
        if bool(args.source) == bool(args.review_set):
            print("identify needs exactly one of --source or --review-set",
                  file=sys.stderr)
            return 2
        _SNIFF_BUCKET = {"video": "Video", "audio": "Audio", "image": "Images"}
        if args.source:
            src_root = cfg.source(args.source).root
            rows = list(store.source_iter(args.source, "REVIEW"))
            all_rels = [r["relpath"] for r in store.source_iter(args.source)]
            items = seam.build_review_set(
                cfg, rows, root=src_root,
                sibling_index=seam.build_sibling_index(all_rels),
                doc_props=hintsmod.doc_props_map(src_root, rows))
            for it in items:
                if it["bucket"] is None and it.get("origin"):
                    kind = sniff.kind_of(it["origin"])
                    if kind:
                        it["bucket"] = _SNIFF_BUCKET[kind]
                        it["content_kind"] = kind
            review_set_path = report.write_review_set(store.workspace, run_id, items)
            print(f"review-set '{args.source}': {len(items)} REVIEW item(s) "
                  f"-> {review_set_path}")
        else:
            review_set_path = args.review_set

        # P21/B2: live-search evidence, assembled once up front (same posture
        # as `mlo agent critics`) so every batch inside identify() sees it.
        _warn_live_search_unconfigured(args, cfg)
        if args.live_search and cfg.enrich.searxng_url:
            from .enrich import evidence as evidencemod
            from .enrich import searxng as searxngmod
            live_items = identifymod.read_review_set(review_set_path)
            evidencemod.assemble(
                live_items, cfg,
                search_fn=searxngmod.search_fn(cfg.enrich.searxng_url))
            review_set_path = report.write_review_set(
                store.workspace, run_id, live_items)

        chain = tuple(c.strip() for c in args.chain.split(",") if c.strip()) \
            if args.chain else None
        merged, res = identifymod.identify(
            cfg, review_set_path, chain=chain, batch_size=args.batch_size,
            cross_check=args.cross_check, prior_hints_path=args.prior_hints,
            progress=lambda phase, info: print(
                f"  [{phase}] " + " ".join(f"{k}={v}" for k, v in info.items())))

        # The schema gate (P21/B6): a malformed merge must never reach a
        # re-plan silently — validate before reporting success.
        stem = args.out.removesuffix(".json")
        hpath = report.write_json(store.workspace, run_id, stem, merged)
        hintsmod.load_hints(hpath)

        print(f"identify: {res.batches} batch(es), {res.items} item(s), "
              f"{res.hinted} hinted, {len(res.unsure)} UNSURE "
              f"(-> Unclassified/human), {len(res.dissent)} tiebreak(s)")
        print(f"hints: {hpath}")
        print(f"next: mlo pilot --hints \"{hpath}\"   "
              f"(or: mlo plan reorganize --hints \"{hpath}\")")
        return 0

    if args.cmd == "snapshot":
        from . import snapshot as snapmod
        snap = snapmod.build_snapshot(store, cfg, under=args.under)
        path = report.write_json(store.workspace, run_id, "state-snapshot", snap)
        print(f"snapshot: {snap['total_files']} files, "
              f"{snap['problem_count']} problem folder(s)")
        for f in [f for f in snap["folders"] if f["problem"]][:10]:
            print(f"  {f['folder']}  ({f['files']} files, {f['bytes']:,}B) "
                  f"-> suspected {f['suspected_home']} (conf {f['confidence']})")
        print(f"  {path}")
        return 0

    if args.cmd == "verify":
        from .verify import verify_library, verify_staging
        f = (verify_library(store, cfg, quick=not args.deep)
             if args.what == "library" else verify_staging(store, cfg))
        for k, v in f.counts().items():
            print(f"{k}: {v}")
        if f.blocking:
            print("BLOCKING: protected content inside staging — resolve before "
                  "any disposal (defect L12)", file=sys.stderr)
            return 3
        return 0

    if args.cmd == "export":
        if args.table == "ops":
            rows = store.export_ops()
            path = report.export_csv(
                store.workspace, run_id, "ops",
                ["rowid", "op_id", "run_id", "plan_id", "kind", "src_display",
                 "dst_display", "pre_size", "state", "detail", "committed_at"],
                rows, {"run": run_id, "journal_pos": store.journal_pos(),
                       "config_hash": cfg.config_hash})
        elif args.table == "files":
            path = report.export_csv(
                store.workspace, run_id, "files",
                ["relpath", "size", "quick_hash", "mtime_ns"],
                store.index_iter(),
                {"run": run_id, "journal_pos": store.journal_pos(),
                 "config_hash": cfg.config_hash})
        else:
            if not args.name:
                print("export source needs a source name", file=sys.stderr)
                return 2
            path = report.export_csv(
                store.workspace, run_id, f"source-{args.name}",
                ["relpath", "size", "quick_hash", "mtime_ns", "verdict",
                 "verdict_rule"],
                store.source_iter(args.name),
                {"run": run_id, "journal_pos": store.journal_pos(),
                 "config_hash": cfg.config_hash})
        print(path)
        return 0

    if args.cmd == "agent":
        return _run_agent(args, cfg, store, run_id)

    raise AssertionError(f"unhandled command {args.cmd}")  # pragma: no cover


def _run_sweep(args, cfg: Config, store: Store, run_id: str) -> int:
    from . import sweep as sweepmod
    entries = sweepmod.sweep(
        store, cfg, run_id, sources=args.sources or None,
        confirm_bytes=args.confirm_mb * 1024 * 1024, execute=args.execute,
        waive_organize=args.waive_organize)
    held = swept = would = confirm_failed = 0
    for e in entries:
        v = e.verdicts
        print(f"  {e.source:<16s} ORG={v.get('ORGANIZED', 0):<5d} "
              f"UNIQ={v.get('UNIQUE', 0):<4d} JUNK={v.get('JUNK', 0):<4d} "
              f"REV={v.get('REVIEW', 0):<4d} | {e.status}")
        held += e.held
        swept += e.staged
        would += e.would_stage
        confirm_failed += e.confirm_failed
    verb, n = ("staged", swept) if args.execute else ("would stage", would)
    print(f"\nsweep: {len(entries)} sources · {verb} {n} originals · "
          f"{held} held (unique, preserve first) · "
          f"{confirm_failed} kept back (failed confirm)")
    exit_code = 3 if (held or confirm_failed) else 0
    report.write_summary(store.workspace, run_id, {
        "command": "sweep", "config_hash": cfg.config_hash,
        "counts": {"sources": len(entries), "staged": swept, "would_stage": would,
                   "held": held, "confirm_failed": confirm_failed},
        "entries": [vars(e) for e in entries], "exit_code": exit_code,
        "suggested_next": [{"cmd": f"mlo plan organize {e.source}",
                            "why": "held originals (unique, preserve first)"}
                           for e in entries if e.held]})
    return exit_code


def _run_agent(args, cfg: Config, store: Store, run_id: str) -> int:
    import glob as globmod
    import json as jsonmod

    from .agent import evals as evalsmod, tasks as tasksmod
    from .agent.llm import ChainClient

    if args.agent_cmd == "eval":
        # --mock uses the deterministic endpoint (CI/offline); a LIVE eval
        # respects the kill-switch like every other agent command (F12): it must
        # not silently hammer localhost when [llm] enabled = false.
        if not args.mock and not cfg.llm.enabled:
            print("agent disabled: [llm] enabled = false — enable it for a live "
                  "eval, or pass --mock for the deterministic harness",
                  file=sys.stderr)
            return 2
        chain = tuple(c.strip() for c in args.chain.split(",") if c.strip()) \
            if args.chain else None
        ecfg = evalsmod.eval_config(cfg, chain=chain)
        transport = evalsmod.heuristic_transport if args.mock else None
        client = ChainClient(ecfg, transport=transport)
        labels = tuple(cfg.taxonomy.keys()) or (
            "Video", "Audio", "Photos", "Documents", "Backups")
        media = tasksmod._media_extensions(cfg) or {
            ".jpg", ".mp4", ".mp3", ".vob", ".amr"}
        results = [
            evalsmod.eval_classify(
                client, os.path.join(args.dir, "classify.json"), labels),
            evalsmod.eval_triage(
                client, os.path.join(args.dir, "triage.json"), media),
        ]
        # P21/B7: the critic-panel eval runner — there was none before this.
        # Optional: only runs when the golden set exists (older evals/ dirs
        # or a --dir override without it just skip this row).
        critics_path = os.path.join(args.dir, "critics.json")
        if os.path.isfile(critics_path):
            results.append(evalsmod.eval_critics(client, ecfg, critics_path))
        # The per-call chain ledger is bulky and byte-heavy; persist it to a
        # write-only JSONL view and keep only a rollup on the printed result.
        ledger = [e for r in results for e in r.pop("ledger", [])]
        ledger_path = report.write_agent_ledger(store.workspace, run_id, ledger)
        chain_summary = report.summarize_ledger(ledger)
        for r in results:
            print(jsonmod.dumps(r, indent=1))
        print(f"chain measured: {list(ecfg.llm.chain)}")
        print(f"chain ledger: {jsonmod.dumps(chain_summary)}  ->  {ledger_path}")
        report.write_summary(store.workspace, run_id, {
            "command": "agent eval", "config_hash": cfg.config_hash,
            "chain": list(ecfg.llm.chain), "chain_ledger": chain_summary,
            "counts": {}, "results": results, "exit_code": 0,
            "suggested_next": []})
        dangerous = sum(r.get("dangerous_errors", 0) for r in results)
        return 3 if dangerous else 0

    if args.agent_cmd == "improve":
        # Deterministic loop (distillation) — no model, no library mutation.
        from . import distill, selfimprove
        fixtures = selfimprove.load_fixtures(args.dogfood)
        if not fixtures:
            print(f"no dogfood fixtures found in {args.dogfood}", file=sys.stderr)
            return 2
        known = selfimprove.load_known_failures(args.known)
        out = selfimprove.improve(cfg, fixtures, known)
        fm = out["after"]["failure_modes"]
        print(f"self-improve: {out['status']}  correct "
              f"{out['before']['correct']}->{out['after']['correct']} "
              f"of {out['after']['total']}")
        print(f"  failure_modes: {jsonmod.dumps(fm)}")
        if out["status"] == "halted":
            print("  HARD STOP: dangerous error or regression — escalated to human",
                  file=sys.stderr)
        if out["rules"]:
            toml = distill.render_patterns_toml(out["rules"])
            tpath = report.write_run_text(store.workspace, run_id,
                                          "distilled-patterns.toml", toml)
            print(f"  proposed rule-diff (review, then merge into mlo.toml): {tpath}")
        report.write_summary(store.workspace, run_id, {
            "command": "agent improve", "config_hash": cfg.config_hash,
            "counts": {"correct_before": out["before"]["correct"],
                       "correct_after": out["after"]["correct"],
                       "total": out["after"]["total"]},
            "failure_modes": fm, "status": out["status"], "rounds": out["rounds"],
            "exit_code": 3 if out["status"] == "halted" else 0,
            "suggested_next": []})
        return 3 if out["status"] == "halted" else 0

    if args.agent_cmd == "critics":
        # Critics carry the heaviest judgment -> they may run on a stronger
        # chain. Resolution: --chain > [llm] critics_chain > [llm] chain.
        # chain_config never touches the enabled kill-switch.
        from .agent.llm import chain_config
        chain = tuple(c.strip() for c in args.chain.split(",") if c.strip()) \
            if getattr(args, "chain", None) else (cfg.llm.critics_chain or None)
        cfg = chain_config(cfg, chain)

    client = ChainClient(cfg)

    if args.agent_cmd == "classify" and args.media:
        import json as jsonmod

        from .config import ConfigError
        if not args.paths:
            print("agent classify --media needs --paths <json list of relpaths>",
                  file=sys.stderr)
            return 2
        try:
            with open(args.paths, encoding="utf-8") as f:
                relpaths = jsonmod.load(f)
        except (OSError, jsonmod.JSONDecodeError) as e:
            raise ConfigError(f"cannot read paths file {args.paths}: {e}")
        if not isinstance(relpaths, list):
            raise ConfigError(f"{args.paths} must be a JSON list of relpaths")
        out = tasksmod.classify_media(client, store, cfg, args.source, relpaths)
        stem = args.out.removesuffix(".json")
        hpath = report.write_json(store.workspace, run_id, stem, out["hints"])
        print(f"media identities: {len(out['hints'])} hinted "
              f"({out['pattern_hits']} by name pattern), "
              f"{len(out['junk'])} junk (stay put), "
              f"{len(out['unsure'])} UNSURE (stay put) of {out['total']}")
        print(f"hints: {hpath}")
        if out["junk"]:
            jpath = report.write_json(store.workspace, run_id,
                                      f"{stem}-junk", out["junk"])
            print(f"junk candidates (for a later triage pass): {jpath}")
        print(f"next: mlo plan reorganize --under <prefix> --hints \"{hpath}\"")
        return 0

    if args.agent_cmd == "classify":
        if not args.source:
            print("agent classify needs a source name (or use --media --paths)",
                  file=sys.stderr)
            return 2
        out = tasksmod.classify_unmatched(client, store, cfg, args.source,
                                          limit=args.limit)
        print(f"classified {out['total']} REVIEW paths: "
              f"{len(out['proposals'])} proposals, {len(out['unsure'])} UNSURE")
        path = report.export_csv(
            store.workspace, run_id, f"classify-{args.source}",
            ["relpath", "label", "confidence", "via"], out["proposals"],
            {"run": run_id, "config_hash": cfg.config_hash,
             "journal_pos": store.journal_pos()})
        print(f"proposals: {path}")
        print("next: add [taxonomy.buckets] rules for labels you accept, then "
              f"re-run: mlo verdicts {args.source}")
        return 0

    if args.agent_cmd == "critics":
        from . import seam, sniff
        from .agent import critics as criticsmod
        from .config import ConfigError
        if bool(args.source) == bool(args.review_set):
            print("agent critics needs exactly one of --source or --review-set",
                  file=sys.stderr)
            return 2
        _SNIFF_BUCKET = {"video": "Video", "audio": "Audio", "image": "Images"}
        if args.source:
            src_root = cfg.source(args.source).root
            rows = list(store.source_iter(args.source, "REVIEW"))
            all_rels = [r["relpath"] for r in store.source_iter(args.source)]
            items = seam.build_review_set(
                cfg, rows, root=src_root,
                sibling_index=seam.build_sibling_index(all_rels),
                doc_props=hintsmod.doc_props_map(src_root, rows))
            # A REVIEW file matched no extension bucket. Content-sniff it: a
            # recovery carve whose extension lies gets a media bucket back, so a
            # critic can engage it (S1). A genuine non-media file stays bucketless
            # and the panel abstains on it — never a guessed identity.
            sniffed = 0
            for it in items:
                if it["bucket"] is None and it.get("origin"):
                    kind = sniff.kind_of(it["origin"])
                    if kind:
                        it["bucket"] = _SNIFF_BUCKET[kind]
                        it["content_kind"] = kind
                        sniffed += 1
            rspath = report.write_review_set(store.workspace, run_id, items)
            print(f"review-set '{args.source}': {len(items)} REVIEW item(s), "
                  f"{sniffed} recovered a media type by content -> {rspath}")
        else:
            items = []
            try:
                with open(args.review_set, encoding="utf-8") as f:
                    for i, line in enumerate(f):
                        line = line.strip()
                        if not line:
                            continue
                        obj = jsonmod.loads(line)
                        if i == 0 and "schema" in obj:
                            continue                 # header line
                        items.append(obj)
            except (OSError, jsonmod.JSONDecodeError) as e:
                raise ConfigError(f"cannot read review-set {args.review_set}: {e}")

        if args.limit and len(items) > args.limit:
            print(f"(sampling the first {args.limit} of {len(items)} review items)")
            items = items[:args.limit]
        # P21/B2: same evidence-assembly path as `mlo pilot` (previously this
        # command ran the panel with NO evidence kwarg at all — closes G124).
        from .enrich import evidence as evidencemod
        _warn_live_search_unconfigured(args, cfg)
        search_fn = None
        if args.live_search and cfg.enrich.searxng_url:
            from .enrich import searxng as searxngmod
            search_fn = searxngmod.search_fn(cfg.enrich.searxng_url)
        evidencemod.assemble(items, cfg, search_fn=search_fn)
        out = criticsmod.run_panel(
            client, cfg, items,
            evidence={it["relpath"]: it.get("evidence", {}) for it in items},
            cross_check=args.cross_check)
        hinted = out["hints"]
        for it in items:                             # every reviewed file, verbatim path
            ab = it.get("origin") or it["relpath"]
            sk = it.get("content_kind")
            h = hinted.get(it["relpath"])
            if h:
                desc = (f"{h.get('media_kind') or 'photo'}  "
                        f"lang={h.get('language')} year={h.get('year')}")
            else:
                why = (f"sniffed {sk}, no confident id" if sk
                       else "non-media / no critic applies")
                desc = f"UNSURE -> Unclassified/human ({why})"
            print(f"  {ab}  ->  {desc}")
        stem = args.out.removesuffix(".json")
        hpath = report.write_json(store.workspace, run_id, stem, out["hints"])
        print(f"critic panel: {len(out['hints'])} hinted, "
              f"{len(out['unsure'])} UNSURE (-> Unclassified/human), "
              f"{len(out['dissent'])} tiebreak(s) of {len(items)} items")
        print(f"hints: {hpath}")
        if out["dissent"]:
            dpath = report.write_json(store.workspace, run_id,
                                      f"{stem}-dissent", out["dissent"])
            print(f"dissent log: {dpath}")
        return 0

    if args.agent_cmd == "triage":
        out = tasksmod.triage_review(client, store, cfg, args.source)
        for d in out["decisions"]:
            print(f"  [{d['disposition']:>12s}] {d['top']}  {d['ext']}  "
                  f"n={d['count']}  {d['bytes']:,}B  conf={d['confidence']:.2f}"
                  f"  — {d['rationale']}")
        if out["guarded"]:
            print(f"({out['guarded']} cluster(s) downgraded to needs-human by the "
                  f"dangerous-error guard)")
        path = report.export_csv(
            store.workspace, run_id, f"triage-{args.source}",
            ["id", "top", "ext", "count", "bytes", "disposition",
             "model_disposition", "rationale", "confidence"],
            out["decisions"],
            {"run": run_id, "config_hash": cfg.config_hash,
             "journal_pos": store.journal_pos()})
        print(f"decisions: {path}")
        return 0

    if args.agent_cmd == "run":
        for step in range(args.steps):
            summaries = sorted(
                globmod.glob(os.path.join(store.workspace, "runs", "*",
                                          "summary.json")),
                key=os.path.getmtime)
            if not summaries:
                print("no summaries yet — run a scan/plan/apply first")
                return 0
            summary = jsonmod.load(open(summaries[-1], encoding="utf-8"))
            choice = tasksmod.next_action(client, summary)
            if choice["choice"] == "stop":
                print(f"agent: stop — {choice['why']}")
                return 0
            cmd = summary["suggested_next"][choice["choice"]]["cmd"]
            print(f"agent picked: {cmd}   ({choice['why']})")
            if not args.act:
                print("(rehearsal — pass --act to dispatch)")
                return 0
            import shlex
            argv = shlex.split(cmd, posix=(os.name != "nt"))
            # posix=False (Windows) keeps the quote characters ON the token
            # ('"I:\\x y.json"'), which then reaches argparse as a literal —
            # strip matched surrounding quotes; backslashes stay intact.
            argv = [t[1:-1] if len(t) >= 2 and t[0] == t[-1]
                    and t[0] in "\"'" else t for t in argv]
            if argv[:1] != ["mlo"]:
                print(f"refusing non-mlo command: {cmd}", file=sys.stderr)
                return 2
            # A suggested command comes from a summary.json on disk; it must not
            # be able to re-point --config (argparse is last-wins, so a forwarded
            # --config would strip PathPolicy) or smuggle any other global flag.
            forwarded = argv[1:]
            if any(tok == "--config" or tok in ("-c", "--version")
                   or tok.startswith("--config=") for tok in forwarded):
                print(f"refusing forwarded global flag in: {cmd}", file=sys.stderr)
                return 2
            code = main(["--config", cfg.path] + forwarded)
            print(f"agent step {step + 1}: exit {code}")
            if code not in (0, 3):
                return code
        return 0

    raise AssertionError(f"unhandled agent command {args.agent_cmd}")


if __name__ == "__main__":
    sys.exit(main())
