"""Pilot — the whole-library analysis pass (Pass 1 of the 2-pass product).

`mlo pilot` runs EVERYTHING read-only + rehearsed and assembles one sealed,
reviewable proposal: scan -> per-source verdicts -> every applicable plan
builder -> full-signal review-set -> critic panel (frontier chain) -> hinted
re-plan -> per-section rehearsal. The human then reviews the proposal (web UI
or file) and Pass 2 (`mlo pilot --execute`, pilot_exec.py in P3) runs exactly
the approved sections with bounded convergence.

Like sweep.py, this module composes the existing gated primitives and adds ZERO
filesystem power: every builder gate (freshness, protected paths, coverage,
C19/C21/C22/C23) still applies; rehearsals go through the same apply_plan code
path as execution; nothing here writes except through report/store. Pass 1
provably leaves the ops journal untouched (tested).

Bounded judgment: critics see at most `critic_limit` items (overflow is counted
and queued for the human — never silently dropped), evidence assembly is the
batched/cached offline path, and per-source REVIEW piles are included in the
review-set artifact for the human residue queue but NOT sent to critics — only
the library's unrouted residue is, because that is the pile whose hints feed an
executable re-plan (a REVIEW file is not planned by organize at all).
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from . import apply as applymod
from . import hints as hintsmod
from . import provenance, report, scan, seam, sniff, verdict
from . import verify as verifymod
from .config import Config
from .plan import (CoverageBlockedError, OrderingError, PlanError,
                   build_containers, build_date_drain, build_dedup,
                   build_dedup_library, build_flatten_provenance,
                   build_organize, build_prune_empty, build_reorganize)
from .store import Store
from .taxonomy import Hints


@dataclass
class Section:
    id: str                          # "organize:G_phone", "reorganize:library"
    kind: str                        # plan kind
    source: str                      # source name or "library"
    status: str                      # ready | gated | blocked | empty
    depends_on: list[str] = field(default_factory=list)
    plan_path: str | None = None
    plan_id: str | None = None
    n_rows: int = 0
    bytes: int = 0                   # sum of row pre.size (0 for rmdir plans)
    rehearsal: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    builder_args: dict = field(default_factory=dict)
    clusters: list[dict] = field(default_factory=list)


@dataclass
class PilotResult:
    proposal_path: str
    sections: list[Section]
    review: dict
    summary_path: str
    exit_code: int = 0


_MOVE_KINDS = ("copy_in", "move_within")


def _cut_at_container(segs: list[str], rule: str) -> list[str]:
    """Label segments for a containers row: the dest path up to and including
    the identity segment (named in the rule 'container:<kind>:<ident>').
    For phone-backups the ident is the DEVICE ('S5'), so a whole device tree
    is one approval cluster no matter which source containers contribute.
    Falls back to the first two segments if the rule shape is unexpected."""
    parts = rule.split(":", 2)
    if len(parts) == 3 and parts[2] in segs:
        return segs[:segs.index(parts[2]) + 1]
    return segs[:2]


def _row_cluster(kind: str, r: dict, lib: str) -> tuple[str, str, str]:
    """(cluster_id, label, rule) for ONE plan row — the single source of the
    clustering rule; cluster_rows and _cluster_id_for_row both call this (the
    two used to duplicate it with a 'MUST mirror' comment).

    Anchoring: move kinds group by destination; stage_move / rmdir_empty by
    source. rmdir rows anchor on the directory itself; file ops drop the
    filename. containers get ONE cluster per container — approval is
    per-cluster, and a unit split across clusters could be half-approved,
    breaking D5 unit atomicity at the approval layer; the label cuts at the
    container's own segment (named in the rule)."""
    rule = r.get("reason", {}).get("rule", "")
    anchor = r["dst"] if r["kind"] in _MOVE_KINDS else r["src"]
    rel = os.path.relpath(anchor, lib) if anchor.startswith(lib) else anchor
    segs = rel.replace("/", os.sep).split(os.sep)
    folder = segs if r["kind"] == "rmdir_empty" else segs[:-1]
    if kind == "containers":
        label = os.sep.join(_cut_at_container(segs, rule))
    else:
        depth = 3 if r["kind"] in _MOVE_KINDS else 2
        label = os.sep.join(folder[:depth]) or segs[0]
    return f"{kind}|{label}|{rule}", label, rule


def cluster_rows(kind: str, rows: list[dict], cfg: Config) -> list[dict]:
    """PURE + deterministic: group plan rows into reviewable clusters. The plan
    file stays the row-level truth; clusters are a VIEW the executor recomputes
    from the sealed plan and verifies by op_ids_sha256 — what the human approved
    is provably what executes."""
    lib = cfg.library_root
    groups: dict[str, dict] = {}
    for r in rows:
        cid, label, rule = _row_cluster(kind, r, lib)
        g = groups.setdefault(cid, {
            "id": cid, "label": label, "rule": rule, "n_rows": 0, "bytes": 0,
            "op_ids": [], "sample": []})
        g["n_rows"] += 1
        g["bytes"] += (r.get("pre") or {}).get("size") or 0
        g["op_ids"].append(r["op_id"])
        if len(g["sample"]) < 5:
            g["sample"].append({"op_id": r["op_id"],
                                "src": r["src"], "dst": r["dst"]})
    out = []
    for g in sorted(groups.values(), key=lambda g: (-g["n_rows"], g["id"])):
        ids = "\n".join(sorted(g.pop("op_ids")))
        g["op_ids_sha256"] = hashlib.sha256(ids.encode("ascii")).hexdigest()
        out.append(g)
    return out


def _hints_jsonable(hmap: dict) -> dict:
    """taxonomy.Hints map -> the hints-JSON shape load_hints reads back."""
    out = {}
    for rel, h in hmap.items():
        d = {}
        if h.media_kind:
            d["media_kind"] = h.media_kind
        if h.language:
            d["language"] = h.language
        if h.year:
            d["year"] = h.year
        if h.content_kind:
            d["content_kind"] = h.content_kind
        if h.book_author:
            d["book_author"] = h.book_author
        if h.book_title:
            d["book_title"] = h.book_title
        if h.book_series:
            d["book_series"] = h.book_series
        if h.book_index is not None:
            d["book_index"] = h.book_index
        if d:
            out[rel.replace(os.sep, "/")] = d
    return out


def _section_from_result(sid: str, kind: str, source: str, res,
                         cfg: Config, builder_args: dict) -> Section:
    _, rows, _ = report.read_plan(res.path)
    return Section(
        id=sid, kind=kind, source=source,
        status="ready" if res.n_rows else "empty",
        plan_path=res.path, plan_id=res.plan_id, n_rows=res.n_rows,
        bytes=sum((r.get("pre") or {}).get("size") or 0 for r in rows),
        notes=list(res.notes), builder_args=builder_args,
        clusters=cluster_rows(kind, rows, cfg))


_SNIFF_BUCKET = {"video": "Video", "audio": "Audio", "image": "Images"}


def analyze(store: Store, cfg: Config, run_id: str, *,
            sources: list[str] | None = None,
            under: list[str] | None = None,
            confirm_bytes: int = 1024 * 1024,
            chain: tuple[str, ...] | None = None,
            critic_limit: int = 500,
            cross_check: bool = False,
            hints_path: str | None = None,
            exif: bool = True,
            sniff_min_mb: float | None = None,
            live_search: bool = False,
            drive_of=None,
            verbose: bool = False,
            progress=None) -> PilotResult:
    """The Pass-1 DAG. Read-only + rehearsed; the ops journal is untouched."""
    under = under or []
    names = list(sources) if sources else [s.name for s in cfg.sources
                                           if s.enabled]

    def step(phase: str, **info):
        if progress:
            progress(phase, info)

    # A1 — library index (stat fast-path; only if stale)
    step("scan-library")
    if not store.artifact_fresh("index:library", cfg.config_hash):
        scan.scan_library(store, cfg, run_id)

    # A2 — per-source scan + verdicts
    verdict_counts: dict[str, dict] = {}
    for name in names:
        step("scan-source", source=name)
        scan.scan_source(store, cfg, name, run_id)
        verdict_counts[name] = verdict.assign(store, cfg, name, run_id)

    # A3 — deterministic hint assembly
    step("hints")
    lib_hints = hintsmod.load_hints(hints_path)
    if exif:
        lib_hints = hintsmod.augment_exif_library(cfg, store, under, lib_hints,
                                                   verbose=verbose)
    if sniff_min_mb is not None:
        lib_hints = hintsmod.augment_sniff_library(cfg, store, under, lib_hints,
                                                   sniff_min_mb, verbose=verbose)
    # C43: book identity (embedded metadata / filename parse) for any Ebooks
    # bucket in scope — unconditional (cheap relative to EXIF), gated on the
    # config actually declaring an Ebooks bucket so a non-P17 config pays
    # nothing extra.
    if hintsmod.book_exts(cfg):
        lib_hints = hintsmod.augment_bookmeta_library(cfg, store, under, lib_hints,
                                                       verbose=verbose)

    # A4 — builders (library reorganize is rebuilt in A7 once critics answer)
    sections: list[Section] = []
    for name in names:
        step("plan", section=f"organize:{name}")
        src_hints = dict(lib_hints)
        if exif:
            src_hints = hintsmod.augment_exif_source(cfg, store, name, src_hints,
                                                      verbose=verbose)
        try:
            res = build_organize(store, cfg, name, drive_of=drive_of,
                                 hints=src_hints)
            sections.append(_section_from_result(
                f"organize:{name}", "organize", name, res, cfg,
                {"kind": "organize", "source": name}))
        except CoverageBlockedError as e:
            sections.append(Section(
                id=f"organize:{name}", kind="organize", source=name,
                status="blocked", blocked_reason=str(e),
                builder_args={"kind": "organize", "source": name}))

        step("plan", section=f"dedup:{name}")
        dedup_args = {"kind": "dedup", "source": name,
                      "confirm_bytes": confirm_bytes}
        try:
            res = build_dedup(store, cfg, name, drive_of=drive_of,
                              confirm_bytes=confirm_bytes)
            sections.append(_section_from_result(
                f"dedup:{name}", "dedup", name, res, cfg, dedup_args))
        except OrderingError:
            # UNIQUE files not yet organized (L13): the dedup plan is built in
            # Pass 2 AFTER the approved organize executes — sweep's semantics.
            # The preview is the verdict counts; the contract the human approves
            # is bounded (only verdict-proven ORGANIZED+JUNK rows can ever be
            # staged, re-confirmed at confirm_bytes).
            v = verdict_counts.get(name, {})
            sec = Section(
                id=f"dedup:{name}", kind="dedup", source=name, status="gated",
                depends_on=[f"organize:{name}"], builder_args=dedup_args,
                notes=[f"gated on organize:{name} (L13); preview from verdicts: "
                       f"ORGANIZED={v.get('ORGANIZED', 0)} "
                       f"JUNK={v.get('JUNK', 0)} would be staged after "
                       f"organize executes"])
            sec.n_rows = v.get("ORGANIZED", 0) + v.get("JUNK", 0)
            sections.append(sec)

    # containers (C33): FIRST among library builders — a declared container is
    # a unit; it claims its subtree before any per-file mover looks. The
    # route() container-member guard keeps reorganize/date-drain out, and
    # flatten has its own containers check, so downstream builders are
    # structurally blind to these files regardless of build order; building
    # first keeps the proposal's story straight.
    step("plan", section="containers:library")
    res = build_containers(store, cfg, under=under or None, drive_of=drive_of)
    sections.append(_section_from_result(
        "containers:library", "containers", "library", res, cfg,
        {"kind": "containers", "under": under}))

    step("plan", section="dedup-library:library")
    res = build_dedup_library(store, cfg, under=under or None,
                              drive_of=drive_of)
    sections.append(_section_from_result(
        "dedup-library:library", "dedup-library", "library", res, cfg,
        {"kind": "dedup-library", "under": under}))

    step("plan", section="reorganize:library")
    reorg_args = {"kind": "reorganize", "under": under}
    # C46: pilot opts every media drain into intrinsic-discriminator collision
    # resolution (build_reorganize/build_date_drain default to False so every
    # standalone `mlo plan` caller keeps skip-and-report).
    reorg = build_reorganize(store, cfg, under=under or None, hints=lib_hints,
                             drive_of=drive_of, disambiguate=True)

    step("plan", section="date-drain:library")
    res = build_date_drain(store, cfg, under=under or None, drive_of=drive_of,
                           disambiguate=True)
    sections.append(_section_from_result(
        "date-drain:library", "date-drain", "library", res, cfg,
        {"kind": "date-drain", "under": under}))

    # prune-empty: a PREVIEW — empties mostly appear only after the moves
    # execute, so Pass 2 rebuilds it fresh after all move sections (L18 makes a
    # rebuilt rmdir plan safe by construction: rmdir cannot remove content).
    step("plan", section="prune-empty:library")
    res = build_prune_empty(store, cfg, under=under or None, drive_of=drive_of)
    prune = _section_from_result(
        "prune-empty:library", "prune-empty", "library", res, cfg,
        {"kind": "prune-empty", "under": under})
    prune.notes.append("preview only — rebuilt fresh in Pass 2 after all "
                       "approved moves execute")

    # flatten-provenance: strip device-origin segments (E_NAS1, G_Phone1, …).
    # Built AFTER the earlier movers so it can exclude their claimed srcs
    # (date-drain already owns Photos\E_NAS1\setup.bmp on the live library) —
    # a src is only ever claimed by one section per pilot run.
    step("plan", section="flatten-provenance:library")
    exclude_srcs: set[str] = set()
    for prior_path in (reorg.path,
                       *(s.plan_path for s in sections
                         if s.kind in ("date-drain", "dedup-library")
                         and s.plan_path)):
        _, prior_rows, _ = report.read_plan(prior_path)
        for r in prior_rows:
            exclude_srcs.add(r["src"])
    flatten_args = {"kind": "flatten-provenance", "under": under}
    flatten = build_flatten_provenance(store, cfg, under=under or None,
                                       exclude_srcs=exclude_srcs,
                                       drive_of=drive_of)
    sections.append(_section_from_result(
        "flatten-provenance:library", "flatten-provenance", "library",
        flatten, cfg, flatten_args))
    # No overlap filter needed against reorg.unrouted: reorg.unrouted is
    # media-bucket only (a bucket exists, no route found) and flatten now
    # skips media-bucket tops entirely (C28). Disjoint by construction.

    # A5 — the full-signal review-set (CANONICAL: critics judge with ALL
    # signals). Library unrouted residue feeds the critics; per-source REVIEW
    # piles join the artifact for the human residue queue.
    step("review-set")
    idx = {r["relpath"]: r for r in store.index_iter()}
    lib_rows = [idx[rel] for rel in reorg.unrouted if rel in idx]
    items = seam.build_review_set(
        cfg, lib_rows, origin_map=provenance.build_origin_map(store),
        sibling_index=seam.build_sibling_index(idx.keys()),
        doc_props=hintsmod.doc_props_map(cfg.library_root, lib_rows))
    review_counts = {"library_unrouted": len(items)}
    for name in names:
        rows = list(store.source_iter(name, "REVIEW"))
        review_counts[f"review:{name}"] = len(rows)
        if not rows:
            continue
        src_root = cfg.source(name).root
        all_rels = [r["relpath"] for r in store.source_iter(name)]
        src_items = seam.build_review_set(
            cfg, rows, root=src_root,
            sibling_index=seam.build_sibling_index(all_rels),
            doc_props=hintsmod.doc_props_map(src_root, rows))
        for it in src_items:
            if it["bucket"] is None and it.get("origin"):
                kind = sniff.kind_of(it["origin"])
                if kind:
                    it["bucket"] = _SNIFF_BUCKET[kind]
                    it["content_kind"] = kind
            it["queue"] = f"review:{name}"       # human residue, not critics
        items.extend(src_items)
    review_set_path = report.write_review_set(store.workspace, run_id, items)

    # A6 — critics on the library unrouted residue (bounded)
    critic_items = [it for it in items if "queue" not in it]
    capped = max(0, len(critic_items) - critic_limit)
    critic_items = critic_items[:critic_limit]
    hinted: dict[str, dict] = {}
    answers: dict[str, dict] = {}
    unsure: list[str] = [it["relpath"] for it in items if "queue" not in it][
        critic_limit:]
    dissent: list[dict] = []
    llm_note = "disabled"
    if cfg.llm.enabled and critic_items:
        step("critics", items=len(critic_items))
        from .agent.critics import run_panel
        from .agent.llm import ChainClient, chain_config
        from .enrich import evidence as evidencemod
        ccfg = chain_config(cfg, chain or (cfg.llm.critics_chain or None))
        client = ChainClient(ccfg)
        # P21/B2: with --live-search and a configured SearXNG instance, the
        # composed query is actually SEARCHED — before this, evidence.assemble
        # was always called with search_fn=None (the "ghost query": a query
        # string was composed and attached, but the internet was never
        # queried). Without it, this stays the offline path (queries only).
        search_fn = None
        if live_search and cfg.enrich.searxng_url:
            from .enrich import searxng as searxngmod
            search_fn = searxngmod.search_fn(cfg.enrich.searxng_url)
        evidencemod.assemble(critic_items, cfg, search_fn=search_fn)  # attaches
                                                  # item['evidence']
        out = run_panel(client, cfg, critic_items,
                        evidence={it["relpath"]: it.get("evidence", {})
                                  for it in critic_items},
                        cross_check=cross_check)
        hinted = out["hints"]
        answers = out["answers"]
        unsure = out["unsure"] + unsure
        dissent = out["dissent"]
        llm_note = ",".join(ccfg.llm.chain)
    elif critic_items:
        unsure = [it["relpath"] for it in critic_items] + unsure
        llm_note = "disabled ([llm] enabled = false) — whole residue to human"

    # A7 — hinted re-plan of reorganize (content-addressed: unchanged -> same id)
    step("replan")
    for rel, h in hinted.items():
        key = rel.replace("/", os.sep)
        prior = lib_hints.get(key) or Hints()

        def _pick(new, old):
            # Field-wise: a critic's explicit None (the photo critic
            # deliberately nulls media_kind) must not erase a deterministic
            # EXIF/sniff signal the edge already established — the same
            # never-clobber rationale the book_* fields always had
            # (super-review A-039).
            return new if new is not None else old

        lib_hints[key] = Hints(
            media_kind=_pick(h.get("media_kind"), prior.media_kind),
            language=_pick(h.get("language"), prior.language),
            year=_pick(h.get("year"), prior.year),
            content_kind=_pick(h.get("content_kind"), prior.content_kind),
            book_author=prior.book_author,
            book_title=prior.book_title,
            book_series=prior.book_series,
            book_index=prior.book_index)
    if hinted:
        reorg = build_reorganize(store, cfg, under=under or None,
                                 hints=lib_hints, drive_of=drive_of,
                                 disambiguate=True)
    sections.append(_section_from_result(
        "reorganize:library", "reorganize", "library", reorg, cfg, reorg_args))
    sections.append(prune)

    # persist the merged hints so Pass-2 convergence re-plans reuse them
    # VERBATIM (no model calls in Pass 2)
    pilot_hints_path = report.write_json(store.workspace, run_id, "pilot-hints",
                                         _hints_jsonable(lib_hints))
    reorg_args["hints_path"] = pilot_hints_path

    # A7.5 — CONVERGENCE REHEARSAL (P16): a naive Pass-1 build sees only the
    # pre-execution index, so each mover's sealed plan is the tip of what
    # actually runs — Pass-2 convergence (rebuilding each section on the
    # progressively-mutated index) does the bulk, unreviewed. Rehearse that
    # whole cycle chain here against a THROWAWAY index copy and SEAL the
    # projected end-state, so what the human approves is what executes. Only
    # the pure-index movers are rehearsable; dedup-library/prune-empty keep
    # their naive plans (noted). Divergence at execute time (index vs reality)
    # is bounded and recorded by Pass-2's convergence audit.
    step("rehearse-convergence")
    by_id = {s.id: s for s in sections}
    rehearse_order = ["containers:library", "dedup-library:library",
                      "reorganize:library", "date-drain:library",
                      "flatten-provenance:library"]
    try:
        projection = _rehearse_projection(
            store, cfg, rehearse_order, by_id, lib_hints, under, drive_of)
    except Exception as e:      # rehearsal is best-effort — never block Pass 1
        projection = {}
        for s in sections:
            if s.kind in _REHEARSE_KINDS:
                s.notes.append(f"convergence rehearsal skipped: "
                               f"{type(e).__name__}: {e}")
    projected_dsts: set[str] = set()   # dsts earlier cycles/sections create
    for sid, proj_rows in projection.items():
        sec = by_id.get(sid)
        if sec is None:
            continue
        projected_dsts.update(os.path.normcase(r["dst"]) for r in proj_rows)
        naive_n = sec.n_rows
        header, _, _ = report.read_plan(sec.plan_path) if sec.plan_path \
            else ({}, [], "")
        inputs = header.get("inputs", []) if header else []
        if proj_rows:
            path, pid = report.write_plan(
                store.workspace, sec.kind, "library", cfg.config_hash,
                inputs, proj_rows)
            sec.plan_path, sec.plan_id = path, pid
            sec.status = "ready"
            sec.n_rows = len(proj_rows)
            sec.bytes = sum((r.get("pre") or {}).get("size") or 0
                            for r in proj_rows)
            sec.clusters = cluster_rows(sec.kind, proj_rows, cfg)
        else:
            sec.status = "empty"
            sec.plan_path = sec.plan_id = None
            sec.n_rows = 0
            sec.clusters = []
        added = sec.n_rows - naive_n
        sec.notes.append(
            f"projected end-state across convergence (P16): {sec.n_rows} rows"
            + (f" (+{added} beyond the first cycle)" if added > 0 else ""))

    # A8 — rehearse every ready section (same code path as execution)
    for sec in sections:
        if sec.status != "ready":
            continue
        step("rehearse", section=sec.id)
        ares = applymod.apply_plan(store, cfg, sec.plan_path, run_id,
                                   execute=False, drive_of=drive_of)
        # A projected plan's beyond-cycle-1 rows reference sources EARLIER
        # projected rows create — on live disk those read as 'source
        # missing', which is the expected convergence chain, not drift. The
        # human-reviewed rehearsal block must not conflate the two
        # (super-review A-037).
        expected_chain = sum(
            1 for d in ares.drift
            if d.get("detail") == "source missing"
            and os.path.normcase(d.get("src", "")) in projected_dsts)
        sec.rehearsal = {"would_do": ares.counts.get("would_do", 0),
                         "skipped_done": ares.counts.get("skipped_done", 0),
                         "drift": ares.counts.get("skipped_drift", 0)
                                  - expected_chain}
        if expected_chain:
            sec.rehearsal["expected_chain"] = expected_chain

    # A9 — assemble the sealed proposal + summary
    step("assemble")
    order = ([f"organize:{n}" for n in names]
             + [f"dedup:{n}" for n in names]
             + ["containers:library", "dedup-library:library",
                "reorganize:library", "date-drain:library",
                "flatten-provenance:library", "prune-empty:library"])
    have = {s.id for s in sections}
    execution_order = [sid for sid in order if sid in have]

    staging_preview: dict[str, dict] = {}
    for sec in sections:
        if sec.kind not in ("dedup", "dedup-library") or not sec.plan_path:
            continue
        _, rows, _ = report.read_plan(sec.plan_path)
        for r in rows:
            if r["kind"] != "stage_move":
                continue
            root = os.path.splitdrive(r["dst"])[0] or "?"
            agg = staging_preview.setdefault(root, {"files": 0, "bytes": 0})
            agg["files"] += 1
            agg["bytes"] += (r.get("pre") or {}).get("size") or 0

    answers_path = report.write_json(store.workspace, run_id, "critic-answers",
                                     answers) if answers else None
    dissent_path = report.write_json(store.workspace, run_id,
                                     "critic-dissent", dissent) \
        if dissent else None

    doc = {
        "created": run_id,
        "config_hash": cfg.config_hash,
        "library_root": cfg.library_root,
        "index": {"files": store.index_count(),
                  "journal_pos": store.journal_pos()},
        "llm": {"enabled": cfg.llm.enabled, "chain": llm_note,
                "critic_items": len(critic_items), "hinted": len(hinted),
                "unsure": len(unsure), "capped": capped},
        "sections": [{
            "id": s.id, "kind": s.kind, "source": s.source, "status": s.status,
            "depends_on": s.depends_on, "plan_path": s.plan_path,
            "plan_id": s.plan_id, "n_rows": s.n_rows, "bytes": s.bytes,
            "rehearsal": s.rehearsal, "notes": s.notes,
            "blocked_reason": s.blocked_reason, "builder_args": s.builder_args,
            "clusters": s.clusters,
        } for s in sections],
        "execution_order": execution_order,
        "review": {"review_set_path": review_set_path,
                   "counts": review_counts,
                   "hinted": len(hinted), "unsure_relpaths": sorted(unsure),
                   "answers_path": answers_path, "dissent_path": dissent_path,
                   "hints_path": pilot_hints_path},
        "staging_preview": staging_preview,
        "convergence": {"max_cycles": 3, "residual_retries": 1},
    }
    proposal_path = report.write_proposal(store.workspace, run_id, doc)

    ready = [s for s in sections if s.status == "ready" and s.n_rows]
    summary_path = report.write_summary(store.workspace, run_id, {
        "counts": {"sections": len(sections),
                   "ready": len(ready),
                   "gated": sum(1 for s in sections if s.status == "gated"),
                   "blocked": sum(1 for s in sections if s.status == "blocked"),
                   "rows": sum(s.n_rows for s in ready),
                   "critic_hinted": len(hinted), "human_queue": len(unsure)},
        "proposal": proposal_path,
        "exit_code": 0,
        "suggested_next": [
            {"cmd": "mlo serve",
             "why": "review the proposal section by section"},
            {"cmd": f'mlo pilot --execute --proposal "{proposal_path}" '
                    f'--approve-all',
             "why": "execute every ready section without the UI"},
        ],
    })
    return PilotResult(proposal_path, sections,
                       doc["review"], summary_path, 0)


# ── Pass 2: execute the approved sections ────────────────────────────────────

APPROVALS_SCHEMA = "mlo.approvals/1"


class ApprovalsError(Exception):
    """Approvals that don't bind to the reviewed proposal. CLI maps to exit 4
    (the approve-X-execute-X gate): re-run `mlo pilot` and re-review."""


@dataclass
class SectionOutcome:
    id: str
    status: str                  # converged | residual | rejected | blocked |
                                 # skipped-empty
    cycles: int = 0
    counts_by_cycle: list = field(default_factory=list)
    unconverged_rows: int = 0
    drift: int = 0
    rejected_dropped: int = 0
    detail: str = ""


@dataclass
class ExecResult:
    outcomes: list[SectionOutcome]
    verify: dict
    staging: dict
    summary_path: str
    exit_code: int = 0


def load_approvals(path: str) -> dict:
    import json
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        raise ApprovalsError(f"unreadable approvals {path}: {e}")
    if doc.get("schema") != APPROVALS_SCHEMA:
        raise ApprovalsError(
            f"unknown approvals schema {doc.get('schema')!r} "
            f"(expected {APPROVALS_SCHEMA})")
    if not isinstance(doc.get("decisions"), dict):
        raise ApprovalsError("approvals need a 'decisions' object")
    return doc


def approve_all(proposal: dict) -> dict:
    """Synthesize approve-everything approvals bound to this proposal —
    persisted by the caller so the audit trail records what was approved."""
    return {"schema": APPROVALS_SCHEMA,
            "proposal_sha256": proposal["proposal_sha256"],
            "decisions": {s["id"]: "approve" for s in proposal["sections"]
                          if s["status"] in ("ready", "gated")},
            "converge": True}


def _decision_for(decisions: dict, sid: str):
    d = decisions.get(sid, "reject")
    if isinstance(d, str):
        return d, {}
    return d.get("default", "reject"), d.get("clusters", {})


def _cluster_id_for_row(kind: str, r: dict, cfg: Config) -> str:
    """The cluster id a single row belongs to — same _row_cluster source as
    cluster_rows, so the two can never drift apart."""
    return _row_cluster(kind, r, cfg.library_root)[0]


def _approved_rows(sec: dict, rows: list[dict], default: str,
                   cluster_decisions: dict, cfg: Config) -> tuple[list, int]:
    """Resolve a partial approval to rows. Clusters are RE-DERIVED from the
    sealed plan and each verified against the proposal's op_ids_sha256 — a
    mismatch refuses the section rather than executing unreviewed rows."""
    derived = {c["id"]: c for c in cluster_rows(sec["kind"], rows, cfg)}
    proposed = {c["id"]: c for c in sec.get("clusters", [])}
    if set(derived) != set(proposed):
        raise ApprovalsError(
            f"section {sec['id']}: plan clusters no longer match the reviewed "
            f"proposal — re-run mlo pilot and re-review")
    for cid, c in derived.items():
        if c["op_ids_sha256"] != proposed[cid]["op_ids_sha256"]:
            raise ApprovalsError(
                f"section {sec['id']}: cluster {cid} differs from the reviewed "
                f"proposal (op set changed) — re-run mlo pilot and re-review")
    keep: list[dict] = []
    rejected = 0
    for r in rows:
        cid = _cluster_id_for_row(sec["kind"], r, cfg)
        if cluster_decisions.get(cid, default) == "approve":
            keep.append(r)
        else:
            rejected += 1
    return keep, rejected


_CONVERGENT = {"reorganize", "date-drain", "dedup-library", "prune-empty",
               "flatten-provenance", "containers"}


def _rebuild(store: Store, cfg: Config, args: dict, drive_of,
             exclude_srcs: set[str] | None = None):
    """Re-run a section's builder from its recorded builder_args (convergence
    re-plans). Hints are reused VERBATIM from Pass 1 — no model calls here."""
    kind = args["kind"]
    under = args.get("under") or None
    if kind == "reorganize":
        h = hintsmod.load_hints(args.get("hints_path"))
        return build_reorganize(store, cfg, under=under, hints=h,
                                drive_of=drive_of, disambiguate=True)
    if kind == "date-drain":
        return build_date_drain(store, cfg, under=under, drive_of=drive_of,
                                disambiguate=True)
    if kind == "dedup-library":
        return build_dedup_library(store, cfg, under=under, drive_of=drive_of)
    if kind == "prune-empty":
        return build_prune_empty(store, cfg, under=under, drive_of=drive_of)
    if kind == "flatten-provenance":
        # Convergence rebuilds keep the one-section-per-src discipline the
        # Pass-1 build had — without it a rebuilt flatten could claim a src
        # that another section's (possibly residual) plan still targets.
        return build_flatten_provenance(store, cfg, under=under,
                                        exclude_srcs=exclude_srcs,
                                        drive_of=drive_of)
    if kind == "containers":
        return build_containers(store, cfg, under=under, drive_of=drive_of)
    if kind == "organize":
        return build_organize(store, cfg, args["source"], drive_of=drive_of)
    if kind == "dedup":
        return build_dedup(store, cfg, args["source"], drive_of=drive_of,
                           confirm_bytes=args.get("confirm_bytes", 0))
    raise PlanError(f"unknown section kind {kind!r}")


def _filter_rejected(rows: list[dict],
                     rejected_srcs: set[str]) -> tuple[list, int]:
    keep = [r for r in rows if os.path.normcase(r["src"]) not in rejected_srcs]
    return keep, len(rows) - len(keep)


def _subset_plan(store: Store, cfg: Config, header: dict, kind: str,
                 source: str, rows: list[dict]) -> str:
    """Write the approved row subset as a NEW sealed plan (the residual-plan
    mechanism): op_ids unchanged (journal idempotency intact), the original
    plan stays on disk as the audit trail of what was proposed."""
    path, _ = report.write_plan(store.workspace, f"{kind}-approved", source,
                                cfg.config_hash, header.get("inputs", []), rows)
    return path


# The library movers that are PURE functions of the index (+ precomputed
# hints), so a Pass-1 index-only rehearsal reproduces exactly what Pass-2
# convergence will do. dedup-library (full-hash) and prune-empty (dir walk)
# read the real filesystem and are NOT index-rehearsable — their naive plans
# stand and their (near-zero) convergence is left to Pass 2.
_REHEARSE_KINDS = ("containers", "reorganize", "date-drain",
                   "flatten-provenance")


def _rehearse_build(store: Store, cfg: Config, kind: str, under, lib_hints,
                    claimed_srcs: set[str], drive_of):
    u = under or None
    if kind == "containers":
        return build_containers(store, cfg, under=u, drive_of=drive_of)
    if kind == "reorganize":
        return build_reorganize(store, cfg, under=u, hints=lib_hints,
                                drive_of=drive_of, disambiguate=True)
    if kind == "date-drain":
        return build_date_drain(store, cfg, under=u, drive_of=drive_of,
                                disambiguate=True)
    if kind == "flatten-provenance":
        return build_flatten_provenance(store, cfg, under=u,
                                        exclude_srcs=set(claimed_srcs),
                                        drive_of=drive_of)
    raise PlanError(f"not a rehearsable kind: {kind!r}")


def _rehearse_projection(store: Store, cfg: Config, order: list[str],
                         sections_by_id: dict, lib_hints: dict, under,
                         drive_of, max_cycles: int = 3) -> dict[str, list]:
    """Pass-1 convergence rehearsal (P16). Replays the pure-index library
    movers in execution order against a THROWAWAY copy of the index — each
    with its full convergence loop, mirroring Pass 2 — so the proposal can
    seal the projected END-STATE, not just the first cycle. Returns
    {section_id: projected_rows}.

    Why this is faithful: the rehearsed builders are pure functions of the
    index and the precomputed hints, and the loop here is the same
    build->apply-effect->rebuild that `execute()` runs; the only difference is
    that `simulate_apply` updates the scratch index instead of the kernel
    updating the real one after a real filesystem move. dedup-library's naive
    plan effect is applied to the scratch (so downstream movers see the
    post-dedup index) but it is not re-converged (full-hash isn't rehearsable;
    its convergence is near-zero anyway).

    Inviolable: touches ONLY the in-memory scratch copy — never the real
    store's connection or the filesystem library."""
    scratch = store.copy_for_rehearsal()
    lib = cfg.library_root
    projected: dict[str, list] = {}
    claimed_srcs: set[str] = set()      # for flatten exclude — mirrors analyze
    try:
        for sid in order:
            sec = sections_by_id.get(sid)
            if sec is None:
                continue
            if sec.kind == "dedup-library":
                if sec.plan_path:
                    _, rows, _ = report.read_plan(sec.plan_path)
                    scratch.simulate_apply(rows, lib)
                    claimed_srcs.update(r["src"] for r in rows)
                continue
            if sec.kind not in _REHEARSE_KINDS:
                continue
            rows_all: list[dict] = []
            seen_ops: set[str] = set()
            cycles = 0
            while cycles < max_cycles:
                res = _rehearse_build(scratch, cfg, sec.kind, under, lib_hints,
                                      claimed_srcs, drive_of)
                _, rows, _ = report.read_plan(res.path)
                fresh = [r for r in rows if r["op_id"] not in seen_ops]
                if not fresh:
                    break
                rows_all.extend(fresh)
                seen_ops.update(r["op_id"] for r in fresh)
                scratch.simulate_apply(fresh, lib)
                claimed_srcs.update(r["src"] for r in fresh)
                cycles += 1
            projected[sid] = rows_all
    finally:
        scratch.close()     # in-memory: closing frees it, nothing to delete
    return projected


def _apply_with_residual(store: Store, cfg: Config, plan_path: str,
                         run_id: str, drive_of) -> tuple[dict, str | None, int]:
    """apply --execute, retrying the residual once (same cycle). Returns
    (counts, residual_plan_or_None, drift)."""
    res = applymod.apply_plan(store, cfg, plan_path, run_id, execute=True,
                              drive_of=drive_of)
    counts = dict(res.counts)
    drift = counts.get("skipped_drift", 0)
    residual = res.residual_plan
    if res.exit_code == 3 and residual:
        res2 = applymod.apply_plan(store, cfg, residual, run_id, execute=True,
                                   drive_of=drive_of)
        for k, v in res2.counts.items():
            counts[k] = counts.get(k, 0) + v
        # The retry re-attempts every drifted row, so summing would count a
        # persistently-drifting row twice; the retry's own drift IS the
        # honest still-drifting number (super-review A-038).
        drift = res2.counts.get("skipped_drift", 0)
        residual = res2.residual_plan
    return counts, residual, drift


def execute(store: Store, cfg: Config, run_id: str, proposal_path: str,
            approvals: dict, *, max_cycles: int = 3,
            drive_of=None, progress=None) -> ExecResult:
    """Pass 2: execute the approved sections in dependency order with bounded
    convergence. NO model calls; hints are Pass-1's, verbatim. Anything that
    does not converge within max_cycles is reported honestly (exit 3), never
    silently retried forever."""
    proposal = report.read_proposal(proposal_path)

    if approvals.get("proposal_sha256") != proposal["proposal_sha256"]:
        raise ApprovalsError(
            "approvals were made against a DIFFERENT proposal — re-run "
            "mlo pilot, review the new proposal, and approve that one")
    if proposal.get("config_hash") != cfg.config_hash:
        from .config import ConfigError
        raise ConfigError(
            "config changed since this proposal was analyzed — re-run "
            "mlo pilot so the review reflects the current config")

    def step(phase: str, **info):
        if progress:
            progress(phase, info)

    store.snapshot()
    decisions = approvals.get("decisions", {})
    converge = bool(approvals.get("converge", True))
    sections = {s["id"]: s for s in proposal["sections"]}
    outcomes: list[SectionOutcome] = []
    rejected_srcs: set[str] = set()
    executed_ok: set[str] = set()

    order = [sid for sid in proposal["execution_order"]
             if sections[sid]["kind"] != "prune-empty"]
    prune_ids = [sid for sid in proposal["execution_order"]
                 if sections[sid]["kind"] == "prune-empty"]

    # Validate EVERY sealed section's approvals UP-FRONT, while the run is
    # still a no-op: an approvals mismatch discovered at section 4 of 6 used
    # to abort mid-run with sections 1-3 already executed. Approval problems
    # are whole-run problems. (The in-loop checks stay — defense in depth.)
    for sid in order:
        sec = sections[sid]
        default, cluster_dec = _decision_for(decisions, sid)
        if default == "reject" and not any(
                v == "approve" for v in cluster_dec.values()):
            continue
        if sec["status"] != "ready" or not sec.get("plan_path"):
            continue
        _, rows, file_plan_id = report.read_plan(sec["plan_path"])
        if file_plan_id != sec["plan_id"]:
            raise ApprovalsError(
                f"plan at {sec['plan_path']} has plan_id "
                f"{file_plan_id[:12]}… but the reviewed proposal sealed "
                f"{sec['plan_id'][:12]}… — refusing to execute")
        if cluster_dec:
            _approved_rows(sec, rows, default, cluster_dec, cfg)

    for sid in order:
        sec = sections[sid]
        default, cluster_dec = _decision_for(decisions, sid)
        if default == "reject" and not any(
                v == "approve" for v in cluster_dec.values()):
            outcomes.append(SectionOutcome(sid, "rejected"))
            if sec.get("plan_path"):
                _, rows, _ = report.read_plan(sec["plan_path"])
                rejected_srcs.update(os.path.normcase(r["src"]) for r in rows)
            continue
        if sec["status"] == "blocked":
            outcomes.append(SectionOutcome(
                sid, "blocked", detail=sec.get("blocked_reason") or ""))
            continue
        if sec["status"] == "empty":
            outcomes.append(SectionOutcome(sid, "skipped-empty"))
            executed_ok.add(sid)
            continue

        step("execute", section=sid)
        out = SectionOutcome(sid, "residual")

        if sec["status"] == "gated":
            # source dedup: built NOW, after its organize executed (the builder
            # re-checks L13 itself — approval covers the bounded contract).
            # Sweep semantics: re-scan + re-verdict the source first — the
            # organize execute changed the library, so the staging proof (which
            # files are now ORGANIZED twins) must be re-derived, not assumed.
            deps_ok = all(d in executed_ok for d in sec.get("depends_on", []))
            try:
                if sec["kind"] == "dedup" and deps_ok:
                    scan.scan_source(store, cfg, sec["source"], run_id)
                    verdict.assign(store, cfg, sec["source"], run_id)
                res = _rebuild(store, cfg, sec["builder_args"], drive_of)
            except (OrderingError, CoverageBlockedError, PlanError) as e:
                out.status = "blocked"
                out.detail = str(e) if deps_ok else \
                    f"dependency not executed: {sec.get('depends_on')}"
                outcomes.append(out)
                continue
            plan_path = res.path
        else:
            plan_path = sec["plan_path"]
            header, rows, file_plan_id = report.read_plan(plan_path)
            if file_plan_id != sec["plan_id"]:
                # 'what was reviewed is provably what executes': the sealed
                # proposal carries the reviewed plan_id — a file swapped at
                # the same path (however valid its own seal) must not run.
                raise ApprovalsError(
                    f"plan at {plan_path} has plan_id {file_plan_id[:12]}… "
                    f"but the reviewed proposal sealed "
                    f"{sec['plan_id'][:12]}… — refusing to execute")
            if cluster_dec:
                keep, rejected = _approved_rows(sec, rows, default,
                                                cluster_dec, cfg)
                out.rejected_dropped = rejected
                keep_ids = {r["op_id"] for r in keep}
                rejected_srcs.update(
                    os.path.normcase(r["src"]) for r in rows
                    if r["op_id"] not in keep_ids)
                if not keep:
                    out.status = "rejected"
                    outcomes.append(out)
                    continue
                if rejected:
                    plan_path = _subset_plan(store, cfg, header, sec["kind"],
                                             sec["source"], keep)

        counts, residual, drift = _apply_with_residual(
            store, cfg, plan_path, run_id, drive_of)
        out.cycles = 1
        out.counts_by_cycle.append(counts)
        out.drift += drift

        # flatten rebuilds keep the one-section-per-src exclusion set the
        # Pass-1 build had: every OTHER section's sealed plan still owns its
        # srcs (a residual row may retry them later).
        flatten_excl: set[str] | None = None
        if sec["kind"] == "flatten-provenance" and converge:
            flatten_excl = set()
            for osid, osec in sections.items():
                if osid != sid and osec.get("plan_path"):
                    _, orows, _ = report.read_plan(osec["plan_path"])
                    flatten_excl.update(r["src"] for r in orows)

        # bounded convergence: idempotent library builders only
        while (converge and sec["kind"] in _CONVERGENT
               and out.cycles < max_cycles):
            res = _rebuild(store, cfg, sec["builder_args"], drive_of,
                           exclude_srcs=flatten_excl)
            _, rows, _ = report.read_plan(res.path)
            rows, dropped = _filter_rejected(rows, rejected_srcs)
            out.rejected_dropped += dropped
            if not rows:
                residual = None
                break
            plan_path = res.path if not dropped else _subset_plan(
                store, cfg, report.read_plan(res.path)[0], sec["kind"],
                sec["source"], rows)
            counts, residual, drift = _apply_with_residual(
                store, cfg, plan_path, run_id, drive_of)
            out.cycles += 1
            out.counts_by_cycle.append(counts)
            out.drift += drift

        if residual:
            _, rrows, _ = report.read_plan(residual)
            out.unconverged_rows = len(rrows)
            out.status = "residual"
        else:
            out.status = "converged"
            executed_ok.add(sid)
        outcomes.append(out)

    # prune-empty last, rebuilt FRESH after all moves (L18: rmdir cannot
    # remove content, so a rebuilt prune under the approved scope is safe)
    for sid in prune_ids:
        sec = sections[sid]
        default, _ = _decision_for(decisions, sid)
        if default != "approve":
            outcomes.append(SectionOutcome(sid, "rejected"))
            continue
        step("execute", section=sid)
        res = _rebuild(store, cfg, sec["builder_args"], drive_of)
        out = SectionOutcome(sid, "converged", cycles=1)
        if res.n_rows:
            counts, residual, drift = _apply_with_residual(
                store, cfg, res.path, run_id, drive_of)
            out.counts_by_cycle.append(counts)
            out.drift = drift
            if residual:
                _, rrows, _ = report.read_plan(residual)
                out.status = "residual"
                out.unconverged_rows = len(rrows)
        if out.status == "converged":
            executed_ok.add(sid)
        outcomes.append(out)

    # verify tail
    step("verify")
    lib_f = verifymod.verify_library(store, cfg, quick=True)
    stg_f = verifymod.verify_staging(store, cfg)
    verify_out = {
        "library": {"unindexed": len(lib_f.unindexed),
                    "missing": len(lib_f.missing),
                    "drifted": len(lib_f.drifted),
                    "mlopart": len(lib_f.mlopart)},
        "staging": {"protected_in_staging": len(stg_f.protected_in_staging),
                    "unjournaled": len(stg_f.unjournaled_staging)},
        "blocking": stg_f.blocking,
    }

    # what actually got staged this run (disposal preview; disposal stays human)
    staging: dict[str, dict] = {}
    for o in outcomes:
        sec = sections.get(o.id, {})
        if sec.get("kind") not in ("dedup", "dedup-library"):
            continue
        done = sum(c.get("done", 0) for c in o.counts_by_cycle)
        if done:
            staging[o.id] = {"staged": done}

    unconverged = [o for o in outcomes if o.status == "residual"]
    blocked = [o for o in outcomes if o.status == "blocked"]
    exit_code = 3 if (unconverged or blocked or verify_out["blocking"]) else 0

    summary_path = report.write_summary(store.workspace, run_id, {
        "counts": {
            "sections_converged": sum(1 for o in outcomes
                                      if o.status == "converged"),
            "sections_residual": len(unconverged),
            "sections_rejected": sum(1 for o in outcomes
                                     if o.status == "rejected"),
            "sections_blocked": len(blocked),
            "rows_done": sum(c.get("done", 0) for o in outcomes
                             for c in o.counts_by_cycle),
            "drift": sum(o.drift for o in outcomes),
        },
        "outcomes": [{
            "id": o.id, "status": o.status, "cycles": o.cycles,
            "unconverged_rows": o.unconverged_rows, "drift": o.drift,
            "rejected_dropped": o.rejected_dropped, "detail": o.detail,
            # P16 audit: rows that executed in convergence cycles BEYOND the
            # first (the approved projection). With Pass-1 rehearsal this is
            # normally 0 — anything here is index-vs-reality divergence that
            # ran without being in the reviewed proposal. Surfaced so the
            # trail is honest, never silent.
            "convergence_delta": sum(
                c.get("done", 0) for c in o.counts_by_cycle[1:]),
        } for o in outcomes],
        "convergence_delta_total": sum(
            sum(c.get("done", 0) for c in o.counts_by_cycle[1:])
            for o in outcomes),
        "verify": verify_out,
        "staging": staging,
        "exit_code": exit_code,
        "suggested_next": (([{"cmd": "mlo verify library --deep",
                              "why": "full-hash reconcile after execution"}]
                            if exit_code == 0 else
                            [{"cmd": "mlo pilot",
                              "why": "re-analyze the unconverged residue"}])
                           + ([{"cmd": "mlo dispose",
                                "why": "staging holds journaled duplicates — "
                                       "build the recycle-bin plan (C68)"}]
                              if any(s.get("staged") for s in staging.values())
                              else [])),
    })
    return ExecResult(outcomes, verify_out, staging, summary_path, exit_code)
