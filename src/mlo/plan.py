"""Plan building: verdicts + taxonomy -> hash-stamped JSONL plans.

Gates enforced HERE, at build time (the strict end of the plan/apply contract):
  - input artifacts must be fresh (defect L7) — exit 4 at the CLI;
  - a dedup (staging) plan requires the source's organize plan to have executed,
    or an explicit waiver (copy-before-stage ordering, defect L13);
  - classifier coverage above threshold blocks an organize plan (defect L4) —
    exit 5 at the CLI;
  - duplicate destinations are rejected outright (defect L17) — destinations
    are unique at plan time, so execute-time name resolution cannot exist.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import (capacity, containers, fingerprint, imgclass, report, staging,
              taxonomy, vidmeta, winpath)
from .config import Config
from .safeops import PathPolicy, op_id_for
from .store import Store
from .verdict import StaleArtifactError


class PlanError(Exception):
    """Structurally invalid plan request. CLI maps to exit 2."""


class OrderingError(Exception):
    """Copy-before-stage violated (L13). CLI maps to exit 2 with remedy."""


class CoverageBlockedError(Exception):
    """Unmatched share above threshold (L4). CLI maps to exit 5."""

    def __init__(self, cov: taxonomy.Coverage, source_name: str):
        self.coverage = cov
        super().__init__(
            f"coverage gate: {cov.unmatched_pct:.1f}% of '{source_name}' matched no "
            f"taxonomy rule (threshold {cov.threshold_pct:.1f}%). Top unmatched "
            f"tokens: {', '.join(t for t, _ in cov.top_unmatched_tokens[:8]) or '-'}. "
            f"Add rules to [taxonomy.buckets] (or raise [classify] "
            f"max_unmatched_pct deliberately) and re-run.")


@dataclass
class PlanResult:
    path: str
    plan_id: str
    n_rows: int
    kind: str
    source: str
    notes: list[str] = field(default_factory=list)
    unrouted: list[str] = field(default_factory=list)   # reorganize: stay-put tail
    confirm_failed: int = 0                              # dedup: twins that failed --confirm-mb


def _require_fresh(store: Store, cfg: Config, artifact_id: str, remedy: str) -> dict:
    if not store.artifact_fresh(artifact_id, cfg.config_hash):
        raise StaleArtifactError(
            f"required artifact '{artifact_id}' is stale or missing — run: {remedy}")
    a = store.artifact_get(artifact_id)
    return {"artifact_id": artifact_id, "journal_pos": a.journal_pos}


def _rows_unique_dsts(rows: list[dict]) -> None:
    seen: dict[str, str] = {}
    for r in rows:
        key = os.path.normcase(r["dst"])
        if key in seen:
            raise PlanError(
                f"duplicate destination in plan (L17): {r['dst']} wanted by both "
                f"{seen[key]} and {r['src']}")
        seen[key] = r["src"]


def _row(kind: str, src: str, dst: str, size: int | None,
         quick_hash: str | None, verdict: str, rule: str) -> dict:
    # None pre-fields are a LOAD-BEARING convention, not an omission: they
    # disable the kernel's execute-time drift check for that row (rmdir rows,
    # undo's chained hops — safeops._precondition_drift).
    return {
        "op_id": op_id_for(kind, src, dst, size, quick_hash),
        "kind": kind,
        "src": src,
        "dst": dst,
        "pre": {"size": size, "quick_hash": quick_hash},
        "reason": {"verdict": verdict, "rule": rule},
    }


def _reject_protected(rows: list[dict], cfg: Config, drive_of=None) -> None:
    """A plan must never contain a protected op — otherwise apply would skip it
    yet the source would look swept (defect L12/C5). Refuse at build, naming the
    offenders, so the operator fixes config or the source before anything runs."""
    kwargs = {"drive_of": drive_of} if drive_of is not None else {}
    policy = PathPolicy(cfg.protected_substrings, cfg.protected_drives,
                        dict(cfg.staging), cfg.library_root, **kwargs)
    hits = []
    for r in rows:
        for end in ("src", "dst"):
            b = policy.check(r[end])
            if b:
                hits.append(f"{r[end]} ({b.reason})")
    if hits:
        raise PlanError(
            "plan would touch protected paths — refusing to build (L12): "
            + "; ".join(hits[:5]) + (" …" if len(hits) > 5 else ""))


def _fresh_inputs(store: Store, cfg: Config, source_name: str) -> list[dict]:
    return [
        _require_fresh(store, cfg, f"scan:{source_name}", f"mlo scan {source_name}"),
        _require_fresh(store, cfg, "index:library", "mlo scan library"),
        _require_fresh(store, cfg, f"verdicts:{source_name}",
                       f"mlo verdicts {source_name}"),
    ]


def _register_plan(store: Store, cfg: Config, kind: str, source_name: str,
                   inputs: list[dict], rows: list[dict],
                   drive_of=None) -> tuple[str, str, list[str]]:
    _reject_protected(rows, cfg, drive_of)
    _rows_unique_dsts(rows)
    path, plan_id = report.write_plan(store.workspace, kind, source_name,
                                      cfg.config_hash, inputs, rows)
    store.artifact_register(f"plan:{plan_id}", "plan",
                            {"kind": kind, "source": source_name, "path": path},
                            cfg.config_hash, run_id="planning", status="fresh")
    return path, plan_id, capacity.preflight_notes(rows, drive_of)


def build_organize(store: Store, cfg: Config, source_name: str,
                   drive_of=None,
                   hints: dict[str, taxonomy.Hints] | None = None) -> PlanResult:
    """UNIQUE files -> content-derived Jellyfin destinations (v0.2 default):
    Movies/Title (Year) under language, Series/Season for TV, year folders for
    photos. Files whose media identity can't be derived (and non-media buckets)
    fall back to the provenance-flat <bucket>/<source>/<relpath> — placement is
    never guessed. hints carries agent classifications / EXIF years, assembled
    by the caller (this builder does no I/O beyond the store)."""
    src_cfg = cfg.source(source_name)
    inputs = _fresh_inputs(store, cfg, source_name)
    hints = hints or {}

    # Coverage gate over everything junk didn't claim (L4).
    non_junk = [r["relpath"] for r in store.source_iter(source_name)
                if r["verdict"] in ("UNIQUE", "REVIEW")]
    cov = taxonomy.coverage(cfg, non_junk)
    if cov.blocked:
        raise CoverageBlockedError(cov, source_name)

    # First pass: compute every row's routed destination, then demote colliding
    # content-routed dests to the provenance-flat fallback (unique by
    # construction). Same-basename media (IMG_0001.jpg in two camera folders)
    # is ordinary data — it must not refuse the whole plan (review C16).
    staged: list[tuple[dict, str, str | None, str]] = []   # (row, src, routed_dst, rule)
    skipped_unbucketed = 0
    for r in store.source_iter(source_name, "UNIQUE"):
        rel = r["relpath"]
        bucket = taxonomy.bucket_for(cfg, rel)
        if bucket is None:
            skipped_unbucketed += 1     # below-threshold tail: stays for review
            continue
        src = os.path.join(src_cfg.root, rel)
        routed_to = taxonomy.route(cfg, rel, hints.get(rel))
        if routed_to is not None:
            staged.append((r, src,
                           os.path.join(cfg.library_root, routed_to.dest_relpath),
                           routed_to.rule))
        else:
            label, rule = bucket
            staged.append((r, src,
                           None, rule))

    routed_counts: dict[str, int] = {}
    for _, _, dst, _ in staged:
        if dst is not None:
            key = os.path.normcase(dst)
            routed_counts[key] = routed_counts.get(key, 0) + 1

    rows, routed, demoted = [], 0, 0
    for r, src, dst, rule in staged:
        if dst is not None and routed_counts[os.path.normcase(dst)] == 1:
            routed += 1
        else:
            if dst is not None:
                demoted += 1
                rule = f"{rule}+flat:collision"
            label, _ = taxonomy.bucket_for(cfg, r["relpath"])
            dst = os.path.join(cfg.library_root, label, source_name, r["relpath"])
        rows.append(_row("copy_in", src, dst, r["size"], r["quick_hash"],
                         "UNIQUE", rule))

    path, plan_id, cap_notes = _register_plan(
        store, cfg, "organize", source_name, inputs, rows, drive_of)
    notes = [f"coverage: {cov.unmatched_pct:.1f}% unmatched "
             f"(threshold {cov.threshold_pct:.1f}%)",
             f"{routed} content-routed (Jellyfin layout), "
             f"{len(rows) - routed} provenance-flat"] + cap_notes
    if demoted:
        notes.append(f"{demoted} routed dests collided (same basename) — "
                     f"placed provenance-flat instead")
    if skipped_unbucketed:
        notes.append(f"{skipped_unbucketed} unique files matched no bucket — "
                     f"left in place for review")
    return PlanResult(path, plan_id, len(rows), "organize", source_name, notes)


# C36 sidecar handling — accessory files that follow their media anchor.
# `<stem>.*` (Sivaji.mkv + Sivaji.srt + Sivaji.nfo + Sivaji-poster.jpg), plus
# well-known accessory basenames living beside media files.
_SIDECAR_ACCESSORIES: frozenset = frozenset({
    "poster.jpg", "poster.jpeg", "poster.png", "poster.tbn",
    "folder.jpg", "folder.png", "folder.tbn",
    "fanart.jpg", "fanart.png",
    "banner.jpg", "banner.png",
    "cover.jpg", "cover.png", "cover.jpeg",
    "back.jpg", "back.png",
    "thumb.jpg", "thumb.png",
    "albumart.jpg", "albumart.png",
    "movie.nfo", "tvshow.nfo", "album.nfo",
    "metadata.opf", "content.opf",     # C43: calibre ebook sidecars
})
_SIDECAR_STEM_PREFIXES: tuple = ("albumart",)      # AlbumArt_{...}_Large.jpg
_SIDECAR_ANCHOR_RULE_PREFIXES: tuple = (
    "route:movie:", "route:tv:", "route:comic:",
    "route:music:", "route:audio:", "route:subtype:", "route:book:")


def _stem_prefix_accessory(low_stem: str) -> bool:
    """Prefix accessories like AlbumArt_{...}_Large.jpg. The prefix must end
    the stem or be followed by a NON-LETTER — a bare startswith claimed every
    'AlbumArtist - …' track as an accessory (validated over-match)."""
    for prefix in _SIDECAR_STEM_PREFIXES:
        if low_stem == prefix or (low_stem.startswith(prefix)
                                  and not low_stem[len(prefix)].isalpha()):
            return True
    return False


def _is_sidecar_of(sib_base: str, sib_stem: str, anchor_stem: str) -> bool:
    """Is sib a sidecar of an anchor named `anchor_stem`? Same stem OR a
    well-known accessory basename OR a stem-prefix accessory (AlbumArt_*)."""
    if sib_stem.casefold() == anchor_stem.casefold():
        return True
    if sib_base.casefold() in _SIDECAR_ACCESSORIES:
        return True
    return _stem_prefix_accessory(sib_stem.casefold())


def _fix_b_discriminator(rel: str, quick_hash: str) -> str:
    """Intrinsic, deterministic discriminator for a content-distinct collision
    (ledger C46): the SOURCE's immediate provenance/parent path segment
    (`E_NAS1`, `G_OldThumbDrive`, …) — human-meaningful and exactly the D12
    owner-disambiguate-under-demonstrated-collision precedent
    (build_containers phase 2). Falls back to the first 6 hex chars of the
    file's own quick_hash when two colliders happen to share that parent
    segment. NEVER a positional counter — the same file gets the same tag on
    every re-plan, regardless of build order (L1's idempotence requirement)."""
    parent = os.path.basename(os.path.dirname(rel.replace("/", os.sep)))
    return parent or quick_hash[:6]


def _tag_dest(dest: str, disc: str) -> str:
    base, ext = os.path.splitext(dest)
    return f"{base} [{disc}]{ext}"


def _resolve_dest_collisions(
    cands: list[tuple[str, dict, str]],       # (rel, row, dest_relpath)
    index_normcased: set[str],
    index_fp: dict[str, tuple[int, str]],
    disambiguate: bool,
) -> tuple[list[tuple[str, dict, str]], int, int, int]:
    """Collision resolution shared by the disambiguation-capable builders
    (ledger C46), mirroring `build_containers` phase 2 (dedup-first, then
    owner/provenance-disambiguate) — the PRECEDENT this generalizes.

    Groups candidates by normcased destination. Within a colliding group,
    partitions by fingerprint (size, quick_hash): byte-identical members are
    a DEDUP decision, never disambiguated — one representative is kept (the
    rest are counted, left for dedup-library); content-distinct members each
    get an intrinsic `_fix_b_discriminator` tag when `disambiguate` is True.
    Disambiguated destinations are re-checked for uniqueness against both the
    library index and each other; a residual clash (the discriminator itself
    collides) skips-and-reports — never overwritten (L1/L17).

    When `disambiguate` is False, a content-distinct collision (or a clash
    against a different-content existing index file) skips-and-reports —
    the prior, still-default behavior every existing test relies on.

    Returns (resolved rows as (rel, row, final_dest), deduped count,
    disambiguated count, collided count)."""
    by_dest: dict[str, list[tuple[str, dict, str]]] = {}
    for c in cands:
        by_dest.setdefault(os.path.normcase(c[2]), []).append(c)

    resolved: list[tuple[str, dict, str]] = []
    deduped = disambiguated = collided = 0
    used: set[str] = set()

    for key in sorted(by_dest):
        group = by_dest[key]
        by_fp: dict[tuple[int, str], list] = {}
        for c in group:
            fp = (c[1]["size"], c[1]["quick_hash"])
            by_fp.setdefault(fp, []).append(c)
        existing_fp = index_fp.get(key)

        reps: list[tuple[str, dict, str]] = []
        for fp, members in by_fp.items():
            members_sorted = sorted(members, key=lambda c: os.path.normcase(c[0]))
            if fp == existing_fp:
                deduped += len(members_sorted)     # all already-there content
                continue
            reps.append(members_sorted[0])
            deduped += len(members_sorted) - 1      # extras: dedup territory

        if not reps:
            continue

        if len(reps) == 1 and key not in index_normcased and key not in used:
            rel, row, dest = reps[0]
            resolved.append((rel, row, dest))
            used.add(key)
            continue

        # A demonstrated collision: >1 content-distinct rep, or the naive
        # dest is occupied by different-content library data.
        if not disambiguate:
            collided += len(reps)
            continue
        for rel, row, dest in sorted(reps, key=lambda c: os.path.normcase(c[0])):
            disc = _fix_b_discriminator(rel, row["quick_hash"])
            tagged = _tag_dest(dest, disc)
            tkey = os.path.normcase(tagged)
            if tkey in used or tkey in index_normcased:
                disc = row["quick_hash"][:6]        # fallback: hash tag
                tagged = _tag_dest(dest, disc)
                tkey = os.path.normcase(tagged)
                if tkey in used or tkey in index_normcased:
                    collided += 1
                    continue
            used.add(tkey)
            resolved.append((rel, row, tagged))
            disambiguated += 1
    return resolved, deduped, disambiguated, collided


def build_reorganize(store: Store, cfg: Config,
                     under: list[str] | None = None,
                     hints: dict[str, taxonomy.Hints] | None = None,
                     drive_of=None,
                     disambiguate: bool = False) -> PlanResult:
    """Library-internal restructuring: move already-indexed files to their
    content-derived destinations via move_within.

    Safety properties (these are the user-facing contract for repairing a real
    library): only files under the `under` prefixes are even examined; a file
    whose route equals its current path yields NO row (idempotence — correctly
    placed trees are structurally untouchable); files with no derivable route
    stay put and are reported; routes without evidence of where the file
    BELONGS are dropped (C19 — no laundering junk into curated trees);
    destination collisions are skipped-and-reported, never resolved by naming
    (L1/L17)."""
    hints = hints or {}
    inputs = [_require_fresh(store, cfg, "index:library", "mlo scan library")]
    prefixes = [p.replace("/", os.sep).rstrip(os.sep) + os.sep
                for p in (under or [])]

    candidates: list[tuple[str, dict, taxonomy.Route]] = []
    unrouted_paths: list[str] = []
    index_normcased: set[str] = set()
    index_fp: dict[str, tuple[int, str]] = {}
    fp_count: dict[tuple[int, str], int] = {}
    # Folder->files map for sidecar lookup (C36). Built in the same pass.
    folder_of: dict[str, list[tuple[str, dict]]] = {}
    already = out_of_scope = no_evidence = provenance_drain = 0
    media_tops = {lbl.casefold() for lbl in taxonomy.MEDIA_LABELS}
    for row in store.index_iter():
        rel = row["relpath"]
        index_normcased.add(os.path.normcase(rel))
        index_fp[os.path.normcase(rel)] = (row["size"], row["quick_hash"])
        fp = (row["size"], row["quick_hash"])
        fp_count[fp] = fp_count.get(fp, 0) + 1
        folder_of.setdefault(
            os.path.dirname(rel).replace("/", os.sep), []).append((rel, row))
        if prefixes and not any(
                os.path.normcase(rel).startswith(os.path.normcase(p))
                for p in prefixes):
            out_of_scope += 1
            continue
        r = taxonomy.route(cfg, rel, hints.get(rel))
        if r is None:
            if taxonomy.bucket_for(cfg, rel) is not None and \
                    os.path.splitext(rel)[1].lower() in _media_exts_for(cfg):
                unrouted_paths.append(rel)   # media the agent could identify
            continue
        if r.rule == "route:book:unsorted":
            # C43: an Ebooks-bucket file with no derivable identity is STILL
            # planned to Books\Unsorted below (an honest shelf, never
            # withheld) but ALSO joins the review list — this is what feeds
            # seam.build_review_set, i.e. what an Opus subagent batch judges
            # (title-only names like 'AdventuresOfHuckleberryFinn.lit' need
            # famous-work knowledge no filename parse has).
            unrouted_paths.append(rel)
        if os.path.normcase(r.dest_relpath) == os.path.normcase(rel):
            already += 1
            continue
        # Evidence rule (C19): reorganize moves a file only when the route says
        # where it BELONGS — a photo without a year going "to Unsorted", or music
        # whose only language is the default shelf, is relocation without evidence
        # and launders recovery junk into curated trees. Those stay put.
        # Scoped-drain relaxation (C23): the EXCEPTION is consolidating a wrong
        # MEDIA root — a scoped (--under) reorganize whose source sits in a media
        # area (Photos\, Videos\, Audio\ dumps; NOT the Other\ recovery pile) is
        # the operator draining misplaced media into the canonical tree, where
        # even an Unsorted landing is the improvement. Unscoped runs, and the
        # non-media recovery pile, keep C19 in full. (organize, placing NEW
        # files, may still use these routes — an entering file needs to land.)
        # Provenance-folder auto-drain (C31): a file directly under a PROVENANCE
        # folder (drive-letter, part, backup/laptop/thumb/hdd) INSIDE a media
        # root is dump-declared — the folder name itself is the drain intent,
        # so a shelf route becomes a home landing without an explicit --under.
        # Still restricted to media tops (Other\ recovery pile is untouched —
        # C19's original concern).
        is_shelf = (r.rule in ("route:photo:unsorted", "route:music:unsorted",
                               "route:video:unsorted")
                    or (r.rule.startswith("route:music:")
                        and r.rule.endswith("lang:default")))
        if is_shelf:
            rel_posix = rel.replace(os.sep, "/")
            src_top = rel_posix.split("/")[0].casefold()
            draining = bool(prefixes) and src_top in media_tops
            if not draining and src_top in media_tops:
                rel_parts = rel_posix.split("/")
                if len(rel_parts) >= 3 and \
                        taxonomy._PROVENANCE_SEG.search(rel_parts[1]):
                    draining = True
                    provenance_drain += 1
            if not draining:
                no_evidence += 1
                continue
        candidates.append((rel, row, r))

    # Duplicate content (C21, found on the first real repair plan): a file
    # whose fingerprint twin exists ANYWHERE in the library — including the
    # candidate set itself — is a dedup decision, not a placement decision.
    # Relocating it would bless redundant copies into curated trees (545 of
    # the first plan's "personal" moves were cross-source consolidation twins).
    kept: list[tuple[str, dict, taxonomy.Route]] = []
    duplicated = 0
    for rel, row, r in candidates:
        if fp_count.get((row["size"], row["quick_hash"]), 0) > 1:
            duplicated += 1
        else:
            kept.append((rel, row, r))
    candidates = kept

    # C36 sidecar handling — collect siblings that belong to a moving media
    # anchor (same stem, or an accessory name like poster.jpg/AlbumArt_*.jpg
    # /.nfo). Runs AFTER C21 dedup (C38 correction): a C21-blocked anchor no
    # longer moves, so its sidecars must not emit as orphan rows either.
    # Sidecars are still EXEMPT from C21 themselves — a shared poster.jpg
    # across many movies would otherwise fail the dedup test and detach from
    # every anchor — they inherit their movement from a SURVIVING anchor.
    # sib_rel -> (row, dst, anchor_rel). The anchor is remembered so emission
    # can be gated on the anchor actually producing a row (C40).
    sidecar_plans: dict[str, tuple[dict, str, str]] = {}
    # Per-folder grouping, built once per folder on first anchor (validator-A
    # perf fix): the old per-anchor scan of every folder member was quadratic
    # — a flat 4000-song dump took 88s in build_reorganize alone.
    _folder_groups: dict[str, tuple[dict, list]] = {}

    def _folder_group(folder: str) -> tuple[dict, list]:
        g = _folder_groups.get(folder)
        if g is None:
            stems: dict[str, list] = {}
            acc: list = []
            for sib_rel, sib_row in folder_of.get(folder, ()):
                base = os.path.basename(sib_rel)
                low_stem = os.path.splitext(base)[0].casefold()
                stems.setdefault(low_stem, []).append((sib_rel, sib_row, base))
                if base.casefold() in _SIDECAR_ACCESSORIES \
                        or _stem_prefix_accessory(low_stem):
                    acc.append((sib_rel, sib_row, base))
            g = _folder_groups[folder] = (stems, acc)
        return g

    for rel, row, r in candidates:
        if not any(r.rule.startswith(p)
                   for p in _SIDECAR_ANCHOR_RULE_PREFIXES):
            continue
        if r.rule.endswith(":already-placed"):
            continue
        if os.path.normcase(r.dest_relpath) == os.path.normcase(rel):
            continue
        anchor_stem = os.path.splitext(os.path.basename(rel))[0]
        anchor_bucket = taxonomy.bucket_for(cfg, rel)
        anchor_label = anchor_bucket[0] if anchor_bucket else None
        src_folder = os.path.dirname(rel).replace("/", os.sep)
        dst_folder = os.path.dirname(r.dest_relpath).replace("/", os.sep)
        stems, acc = _folder_group(src_folder)
        for sib_rel, sib_row, sib_base in \
                stems.get(anchor_stem.casefold(), []) + acc:
            if sib_rel == rel or sib_rel in sidecar_plans:
                continue
            sib_stem = os.path.splitext(sib_base)[0]
            if not _is_sidecar_of(sib_base, sib_stem, anchor_stem):
                continue
            # C41: a same-stem sibling in the anchor's OWN bucket is an
            # alternate copy (.mkv beside .mp4, a re-encode) — a dedup/
            # placement decision, never an accessory. Letting it ride as a
            # sidecar bypassed C21 for duplicate media. Cross-bucket
            # same-stem (cover .jpg beside a movie, .srt with no bucket)
            # and accessory names stay sidecars.
            if sib_stem.casefold() == anchor_stem.casefold() \
                    and sib_base.casefold() not in _SIDECAR_ACCESSORIES:
                sib_bucket = taxonomy.bucket_for(cfg, sib_rel)
                if sib_bucket and anchor_label \
                        and sib_bucket[0] == anchor_label:
                    continue
            sidecar_dst = os.sep.join([dst_folder, sib_base])
            if os.path.normcase(sidecar_dst) == os.path.normcase(sib_rel):
                continue                               # already at dest
            sidecar_plans[sib_rel] = (sib_row, sidecar_dst, rel)

    # A file just scheduled as a sidecar drops from the main candidate set —
    # the sidecar rule wins over its own per-file route.
    candidates = [c for c in candidates if c[0] not in sidecar_plans]

    # Destination collisions: leaving both in place is the only honest default
    # (skip-and-report). Occupancy is tested against the NORMCASED index (a
    # case-variant twin is occupied on Windows and would drift forever if
    # planned — review C17).
    #
    # C46 (ledger): when `disambiguate` is on, a demonstrated content-distinct
    # collision instead gets each member an intrinsic discriminator (the
    # source's own provenance/parent segment — see _resolve_dest_collisions);
    # C21 above has already removed byte-identical twins from `candidates`
    # entirely, so every collision reaching this point is content-distinct by
    # construction. Off by default — every existing skip-and-report test is
    # unaffected; pilot opts in for the media drains.
    rows = []
    planned_dsts: set[str] = set()
    moved_srcs: set[str] = set()
    rule_of = {rel: r.rule for rel, _, r in candidates}
    cands_dd = [(rel, row, r.dest_relpath) for rel, row, r in candidates]
    resolved, _dd, disambiguated, collided = _resolve_dest_collisions(
        cands_dd, index_normcased, index_fp, disambiguate)
    for rel, row, dest in resolved:
        key = os.path.normcase(dest)
        rows.append(_row("move_within",
                         os.path.join(cfg.library_root, rel),
                         os.path.join(cfg.library_root, dest),
                         row["size"], row["quick_hash"], "REORGANIZE",
                         rule_of[rel]))
        planned_dsts.add(key)
        moved_srcs.add(rel)

    # C36: emit sidecar rows AFTER main rows. Sidecars use standard
    # collision handling (dest occupied → skip) but are EXEMPT from C21
    # twin-skip. If two anchors bring the same sidecar to the same folder,
    # first-planned wins; the loser skips as a normal collision.
    # C40: a sidecar emits ONLY when its anchor actually produced a row —
    # an anchor dropped by the collision loop above must not shed its
    # sidecars into the occupied folder (same orphan class as C38, reached
    # through the collision path instead of C21).
    sidecars_moved = sidecars_collided = sidecars_anchor_stayed = 0
    for sib_rel, (sib_row, sidecar_dst, anchor_rel) in \
            sorted(sidecar_plans.items()):
        if anchor_rel not in moved_srcs:
            sidecars_anchor_stayed += 1
            continue
        key = os.path.normcase(sidecar_dst)
        if key in index_normcased or key in planned_dsts:
            sidecars_collided += 1
            continue
        rows.append(_row("move_within",
                         os.path.join(cfg.library_root, sib_rel),
                         os.path.join(cfg.library_root, sidecar_dst),
                         sib_row["size"], sib_row["quick_hash"],
                         "SIDECAR", "route:sidecar:with-anchor"))
        planned_dsts.add(key)
        sidecars_moved += 1

    path, plan_id, cap_notes = _register_plan(
        store, cfg, "reorganize", "library", inputs, rows, drive_of)
    in_scope = (len(candidates) + duplicated + already
                + len(unrouted_paths) + no_evidence)
    notes = [f"in scope: {in_scope}, "
             f"moves: {len(rows)}, already placed: {already}, "
             f"no derivable route (stay put): {len(unrouted_paths)}, "
             f"no-evidence relocation (stay put): {no_evidence}, "
             f"duplicate content (stay put): {duplicated}, "
             f"collisions (stay put): {collided}"] + cap_notes
    if provenance_drain:
        notes.append(f"C31 provenance auto-drain (media root, into "
                     f"Unsorted): {provenance_drain} files")
    if disambiguate:
        notes.append(f"C46 disambiguated (content-distinct collisions): "
                     f"{disambiguated}")
    if sidecars_moved or sidecars_collided or sidecars_anchor_stayed:
        notes.append(f"C36 sidecars moved with anchor: {sidecars_moved}, "
                     f"skipped by collision: {sidecars_collided}, "
                     f"stayed with unmoved anchor (C40): "
                     f"{sidecars_anchor_stayed}")
    if prefixes:
        notes.append("scoped under: " + ", ".join(under))
    return PlanResult(path, plan_id, len(rows), "reorganize", "library", notes,
                      unrouted=unrouted_paths)


_EPOCH_MS = re.compile(r"^1\d{12}$")


def _capture_dt(basename: str, mtime_ns: int | None):
    """Best capture time for a residue photo: a 13-digit epoch-ms FILENAME (a
    real capture timestamp) if it has one, else the filesystem mtime. Returns a
    UTC datetime, or None if neither is usable.

    NOTE (accepted limitation, C24): for a recovery carve the mtime is the
    COPY/recovery date, not the capture date — so a mtime-derived year bucket
    (Images/Photos/<year>) may be wrong. Deliberately accepted to drain the
    residue; a future EXIF/vision pass can correct the year in place."""
    import datetime
    stem = os.path.splitext(basename)[0]
    if _EPOCH_MS.match(stem):
        try:
            return datetime.datetime.fromtimestamp(
                int(stem) / 1000, datetime.timezone.utc)
        except (ValueError, OSError, OverflowError):
            pass
    if not mtime_ns:
        return None
    try:
        return datetime.datetime.fromtimestamp(
            mtime_ns / 1e9, datetime.timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def build_date_drain(store: Store, cfg: Config,
                     under: list[str] | None = None,
                     drive_of=None,
                     disambiguate: bool = False) -> PlanResult:
    """Resolve the same-basename PHOTO collision residue in a wrong media root by
    placing each photo at Images/Photos/<year>/<stem>_<YYYYMMDD_HHMMSS><ext> —
    the year and timestamp are the file's own capture time (a 13-digit epoch-ms
    name if present, else the filesystem mtime, which for a recovery carve is the
    copy date, deliberately accepted). The timestamp plus the original stem
    disambiguate the collisions the flat drain left behind; a residual same-name
    clash gets a numeric suffix (unchanged pre-existing behavior — the built-in
    timestamp already disambiguates almost every clash; see `disambiguate` below
    for the C46 alternative). Fingerprint twins (size+quick_hash) and
    zero-byte files are LEFT for the dedup/stage path (C21 — a duplicate is never
    drained as if distinct). Scoped by --under to the wrong roots; idempotent —
    once a photo sits under Images/Photos it is out of every wrong-root scope.

    C32 scope repair: date-drain now consults taxonomy.route() and refuses
    to touch files that are NOT photo-shelf residue — cross-type sidecars
    (album art in Music, posters in Movies), canonical non-photo image homes
    (Graphics_Icons, WhatsApp, Screenshots, Personal), files imgclass says
    aren't photos, and files already home in a curated year-tree. Without
    this the extension-only filter ripped album art out of Music and
    hierarchically-curated year folders (year/year-month/camera/) flat.

    C45 (personal-media drain): ALSO drains video files sitting in a
    provenance/non-year folder under `layout.personal_root` (device dumps
    like `Video\\Personal\\G_Dashcam\\…`) to `Video\\Personal\\<Year>\\
    <filename>` — a symmetric, video-flavored twin of the photo drain above.
    Capture year precedence (a live defect, fixed 2026-07-15): a STRONGLY-
    structured name date (`imgclass.structured_name_year` — WhatsApp
    `VID-YYYYMMDD-WA####` or a leading 14-digit device stamp) is checked
    FIRST and wins over the embedded container date, because a WhatsApp
    re-encode writes a bogus constant `mvhd` creation time while the
    filename the device wrote is trustworthy; else vidmeta.creation_year
    (the MP4/MOV `mvhd` atom); else a looser name-embedded epoch-ms date
    (imgclass.name_year). A video with NO date signal anywhere is never
    guessed (mtime is never used — C19, a copy resets it) — instead it
    drains to the `personal_root\\Undated\\<filename>` holding shelf
    (`route:personal:undated`), dropping the device-name provenance without
    inventing a false date. A file already at `personal_root\\<Year>\\…` or
    `personal_root\\Undated\\…` yields no row (idempotent); non-video
    sidecars are untouched (they ride their anchor, C36) since only
    Video-bucket extensions are ever candidates here.

    C46 (`disambiguate`, default False): when a destination collision is
    content-distinct (never byte-identical — C21 territory), route the row
    through the intrinsic-discriminator scheme (`_resolve_dest_collisions`,
    the source's own provenance/parent segment) INSTEAD OF the numeric
    suffix above. Off by default so every existing test's numeric-suffix
    behavior is byte-for-byte preserved; pilot opts in for the media drains."""
    inputs = [_require_fresh(store, cfg, "index:library", "mlo scan library")]
    lib = cfg.library_root
    prefixes = [p.replace("/", os.sep).rstrip(os.sep) + os.sep for p in (under or [])]
    photo_exts = set(cfg.taxonomy.get("Photos", ())) | set(cfg.taxonomy.get("Images", ()))
    photos_root = cfg.layout.photos_root.replace("/", os.sep)
    video_exts = set(cfg.taxonomy.get("Video", ())) | set(cfg.taxonomy.get("Videos", ()))
    personal_root = cfg.layout.personal_root.replace("/", os.sep)
    personal_root_posix = cfg.layout.personal_root.replace("\\", "/").strip("/")

    rowset = list(store.index_iter())
    fp_count: dict[tuple[int, str], int] = {}
    index_norm: set[str] = set()
    index_fp: dict[str, tuple[int, str]] = {}
    for r in rowset:
        fp_count[(r["size"], r["quick_hash"])] = \
            fp_count.get((r["size"], r["quick_hash"]), 0) + 1
        index_norm.add(os.path.normcase(r["relpath"]))
        index_fp[os.path.normcase(r["relpath"])] = (r["size"], r["quick_hash"])

    # C32: only these route rules mean "photo shelf residue that needs a year
    # home." Everything else — cross-type sidecars, canonical image homes,
    # non-photo classification, already-placed under a year folder — is out of
    # date-drain's business.
    _DRAINABLE_RULES = {
        "route:photo:unsorted",         # a shelf, the whole reason date-drain exists
        "route:photo:name-year",        # a file with an embedded date but no year folder
        "route:photo:exif-year",        # EXIF-year home (hint-driven)
    }

    cands: list[tuple[str, dict, str]] = []      # (rel, row, dest_rel)
    skipped_dup = skipped_nodate = skipped_scoped = 0
    for row in rowset:
        rel = row["relpath"]
        if prefixes and not any(
                os.path.normcase(rel).startswith(os.path.normcase(p))
                for p in prefixes):
            continue
        if os.path.splitext(rel)[1].lower() not in photo_exts:
            continue
        if row["size"] == 0 or fp_count[(row["size"], row["quick_hash"])] > 1:
            skipped_dup += 1                     # zero-byte / fp-twin -> dedup path
            continue
        # C32 scope check: refuse anything the taxonomy doesn't call a
        # drainable photo shelf. Reasons include the file already sitting in
        # a curated year tree, being an album-art sidecar, being classified
        # as UI/screenshot/whatsapp, or living in a canonical non-photo home.
        r = taxonomy.route(cfg, rel, None)
        if r is None or r.rule not in _DRAINABLE_RULES:
            skipped_scoped += 1
            continue
        dt = _capture_dt(os.path.basename(rel), row["mtime_ns"])
        if dt is None:
            skipped_nodate += 1
            continue
        stem, ext = os.path.splitext(os.path.basename(rel))
        name = f"{stem}_{dt.strftime('%Y%m%d_%H%M%S')}{ext}"
        cands.append((rel, row, os.path.join(photos_root, str(dt.year), name)))

    # C45 — personal-media video drain: a Video-bucket file under
    # personal_root that is NOT already at personal_root\<Year>\… (a
    # provenance folder like G_Dashcam\, or any other non-year subfolder, or
    # loose at the root) drains to personal_root\<Year>\<filename>. Scope
    # guard: only files actually under personal_root are ever candidates —
    # route() itself never applies here (it deliberately treats everything
    # under personal_root as pure human placement), so this scope check IS
    # the guard, mirrored on the photo branch's C32 discipline. Non-video
    # sidecars never enter (only video_exts are considered) — they ride
    # their anchor per C36.
    vcands: list[tuple[str, dict, str, str]] = []   # (rel, row, dest, rule)
    skipped_video_home = 0
    for row in rowset:
        rel = row["relpath"]
        if prefixes and not any(
                os.path.normcase(rel).startswith(os.path.normcase(p))
                for p in prefixes):
            continue
        if os.path.splitext(rel)[1].lower() not in video_exts:
            continue
        rel_posix = rel.replace(os.sep, "/")
        inner = taxonomy._inside(rel_posix, personal_root_posix)
        if inner is None:
            continue                    # not under Video/Personal at all
        parts = inner.split("/")
        if len(parts) > 1 and ((parts[0].isdigit() and len(parts[0]) == 4)
                               or parts[0] == "Undated"):
            # already under personal_root\<Year>\... or \Undated\... at ANY
            # depth — a curated year subtree (2019/Trip/...) is placed; only
            # depth==2 was honored before, silently proposing a flatten of
            # deeper hand-curated structure (super-review M6)
            skipped_video_home += 1
            continue
        if row["size"] == 0 or fp_count[(row["size"], row["quick_hash"])] > 1:
            skipped_dup += 1            # zero-byte / fp-twin -> dedup path
            continue
        # Precedence (C45 fix): a strongly-structured NAME date wins over the
        # embedded mvhd date — a WhatsApp re-encode writes a bogus constant
        # mvhd creation time, but the filename the device wrote is
        # trustworthy. Only after that fails do we consult the container,
        # then a looser name-embedded epoch-ms date.
        basename = os.path.basename(rel)
        year = imgclass.structured_name_year(basename)
        if year is None:
            year = vidmeta.creation_year(os.path.join(lib, rel))
        if year is None:
            year = imgclass.name_year(basename)
        if year is not None:
            vcands.append((rel, row, os.path.join(
                personal_root, str(year), basename), "route:personal:video-date"))
        else:
            # No date signal anywhere -> never guess (C19: mtime is not a
            # date). Drop the device-name provenance to a holding shelf
            # instead of leaving it in the device folder or flat-piling it.
            vcands.append((rel, row, os.path.join(
                personal_root, "Undated", basename), "route:personal:undated"))

    rule_of: dict[str, str] = {rel: "route:photo:date" for rel, _, _ in cands}
    rule_of.update({rel: rule for rel, _, _, rule in vcands})
    all_cands = cands + [(rel, row, dest) for rel, row, dest, _ in vcands]

    rows: list[dict] = []
    disamb_note = 0
    if disambiguate:
        # C46: intrinsic-discriminator resolution instead of the numeric
        # suffix — see _resolve_dest_collisions.
        resolved, _dd, disamb_note, _coll = _resolve_dest_collisions(
            all_cands, index_norm, index_fp, True)
        for rel, row, dest in resolved:
            rows.append(_row("move_within",
                             os.path.join(lib, rel), os.path.join(lib, dest),
                             row["size"], row["quick_hash"], "DATE-DRAIN",
                             rule_of[rel]))
    else:
        # Collision resolution (unchanged, pre-existing): a clash is between
        # SHA-distinct content (twins were excluded above), so a numeric
        # suffix keeps both; deterministic by src path.
        used = set(index_norm)
        for rel, row, dest_rel in sorted(
                all_cands, key=lambda c: os.path.normcase(c[0])):
            d = dest_rel
            if os.path.normcase(d) in used:
                base, ext = os.path.splitext(dest_rel)
                i = 2
                while os.path.normcase(f"{base}_{i}{ext}") in used:
                    i += 1
                d = f"{base}_{i}{ext}"
            used.add(os.path.normcase(d))
            rows.append(_row("move_within",
                             os.path.join(lib, rel), os.path.join(lib, d),
                             row["size"], row["quick_hash"], "DATE-DRAIN",
                             rule_of[rel]))

    n_photo_rows = sum(1 for r in rows if r["reason"]["rule"] == "route:photo:date")
    n_video_dated_rows = sum(
        1 for r in rows if r["reason"]["rule"] == "route:personal:video-date")
    n_video_undated_rows = sum(
        1 for r in rows if r["reason"]["rule"] == "route:personal:undated")
    path, plan_id, cap_notes = _register_plan(
        store, cfg, "date-drain", "library", inputs, rows, drive_of)
    notes = [f"photos placed by capture date: {n_photo_rows}, "
             f"personal videos placed by capture date (C45): {n_video_dated_rows}, "
             f"personal videos routed to Undated shelf (C45): "
             f"{n_video_undated_rows}, "
             f"left for dedup (zero-byte / fingerprint twins): {skipped_dup}, "
             f"already home / not a drainable photo (C32): {skipped_scoped}, "
             f"already home (personal video, C45): {skipped_video_home}, "
             f"no date signal (stay put): {skipped_nodate}"] + cap_notes
    if disambiguate:
        notes.append(f"C46 disambiguated (content-distinct collisions): "
                     f"{disamb_note}")
    if under:
        notes.append("scoped under: " + ", ".join(under))
    return PlanResult(path, plan_id, len(rows), "date-drain", "library", notes)


def build_relocate(store: Store, cfg: Config, mapping: dict[str, str],
                   drive_of=None) -> PlanResult:
    """Library-internal moves from an EXPLICIT relpath -> dest_relpath mapping —
    the execution half of a critic-judged (human-gated) placement pass, e.g. the
    document topic sort. The judgment lives in the mapping artifact; this
    builder only enforces the standard guards:

      - every source must be a current index row (a stale path refuses the
        whole plan — no partial application of a reviewed mapping);
      - dest must differ from src (identity rows are dropped — idempotent);
      - fingerprint twins stay put (C21 — duplicate content is a dedup
        decision, even when a mapping names it);
      - occupied/colliding destinations stay put and are counted (C17);
      - protected paths refuse at build (via _register_plan)."""
    inputs = [_require_fresh(store, cfg, "index:library", "mlo scan library")]
    index: dict[str, dict] = {}
    index_normcased: set[str] = set()
    fp_count: dict[tuple[int, str], int] = {}
    for row in store.index_iter():
        index[os.path.normcase(row["relpath"])] = row
        index_normcased.add(os.path.normcase(row["relpath"]))
        fp = (row["size"], row["quick_hash"])
        fp_count[fp] = fp_count.get(fp, 0) + 1

    missing = [rel for rel in mapping if os.path.normcase(rel) not in index]
    if missing:
        raise PlanError(
            f"{len(missing)} mapped path(s) are not in the library index — "
            f"refusing to build (rescan or fix the mapping): "
            + "; ".join(missing[:5]) + (" …" if len(missing) > 5 else ""))

    already = duplicated = collided = 0
    cands: list[tuple[dict, str]] = []
    for rel, dest in mapping.items():
        row = index[os.path.normcase(rel)]
        if os.path.normcase(dest) == os.path.normcase(row["relpath"]):
            already += 1
            continue
        if fp_count[(row["size"], row["quick_hash"])] > 1:
            duplicated += 1                  # C21: a twin is a dedup decision
            continue
        cands.append((row, dest))

    dest_count: dict[str, int] = {}
    for _, dest in cands:
        key = os.path.normcase(dest)
        dest_count[key] = dest_count.get(key, 0) + 1
    rows = []
    for row, dest in cands:
        key = os.path.normcase(dest)
        if dest_count[key] > 1 or key in index_normcased:
            collided += 1
            continue
        rows.append(_row("move_within",
                         os.path.join(cfg.library_root, row["relpath"]),
                         os.path.join(cfg.library_root, dest),
                         row["size"], row["quick_hash"], "RELOCATE",
                         "relocate:map"))

    path, plan_id, cap_notes = _register_plan(
        store, cfg, "relocate", "library", inputs, rows, drive_of)
    notes = [f"mapped: {len(mapping)}, moves: {len(rows)}, "
             f"already placed: {already}, "
             f"duplicate content (stay put): {duplicated}, "
             f"collisions (stay put): {collided}"] + cap_notes
    return PlanResult(path, plan_id, len(rows), "relocate", "library", notes)


def build_prune_empty(store: Store, cfg: Config,
                      under: list[str] | None = None,
                      drive_of=None) -> PlanResult:
    """Emit rmdir_empty ops for empty directories under the given prefixes,
    deepest-first — a directory emptied only by removing its (also-empty)
    children is removed in the same plan. rmdir refuses a non-empty directory,
    so a staying file (a collision, a duplicate, junk) keeps its whole ancestor
    chain, and a mis-ordered or racing row fails safe, never recursive (L18).
    Scoped by --under to the drained wrong media roots; the scoped root itself is
    never removed. The only I/O here is a read-only directory walk."""
    from . import winpath
    inputs = [_require_fresh(store, cfg, "index:library", "mlo scan library")]
    lib = cfg.library_root
    prefixes = [p.replace("/", os.sep).strip(os.sep) for p in (under or [])]
    roots = [os.path.join(lib, p) for p in prefixes] if prefixes else [lib]

    prunable: set[str] = set()          # normcased dirs that will be empty post-plan
    rows: list[dict] = []
    for root in roots:
        lroot = winpath.to_long(root)
        if not os.path.isdir(lroot):
            continue
        for ldirpath, dirnames, filenames in os.walk(lroot, topdown=False):
            path = winpath.from_long(ldirpath)
            if os.path.normcase(path) == os.path.normcase(root):
                continue                # keep the scoped root itself
            if filenames:
                continue                # a file stays here -> keep the directory
            if any(os.path.normcase(os.path.join(path, d)) not in prunable
                   for d in dirnames):
                continue                # a kept subdir -> keep the directory
            prunable.add(os.path.normcase(path))
            rows.append(_row("rmdir_empty", path, path, None, None,
                             "PRUNE", "prune:empty"))

    plan_path, plan_id, cap_notes = _register_plan(
        store, cfg, "prune-empty", "library", inputs, rows, drive_of)
    notes = [f"empty directories to remove: {len(rows)}"] + cap_notes
    if prefixes:
        notes.append("scoped under: " + ", ".join(under))
    return PlanResult(plan_path, plan_id, len(rows), "prune-empty", "library", notes)


# C37 bad-archive detection — header magic + a stdlib-only zip TOC read.
_ARCHIVE_MAGIC: dict[str, tuple[bytes, ...]] = {
    ".zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".cbz": (b"PK\x03\x04", b"PK\x05\x06"),
    ".rar": (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00"),
    ".cbr": (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00"),
    ".7z":  (b"7z\xbc\xaf\x27\x1c",),
    ".cb7": (b"7z\xbc\xaf\x27\x1c",),
    ".gz":  (b"\x1f\x8b",),
    ".bz2": (b"BZ",),
    ".xz":  (b"\xfd7zXZ\x00",),
    ".cab": (b"MSCF",),
}
_ARCHIVE_EXTS_DEFAULT: frozenset = frozenset(
    list(_ARCHIVE_MAGIC) + [".tar", ".iso"])   # tar/iso magic is offset-based


def _check_archive_integrity(path: str, ext: str) -> str | None:
    """Return a rejection reason for a bad archive, or None if it looks OK.
    Cheap checks only: header magic + zip-TOC readability (no decompression)."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return "unreadable"
    if not head:
        return "empty"
    magics = _ARCHIVE_MAGIC.get(ext, ())
    if magics and not any(head.startswith(m) for m in magics):
        return "bad-magic"
    if ext in (".zip", ".cbz"):
        import zipfile
        try:
            with zipfile.ZipFile(path, "r") as zf:
                zf.namelist()               # forces central-directory read
        except (zipfile.BadZipFile, OSError, KeyError, RuntimeError):
            return "bad-zip-toc"
    return None


def _staging_dest(cfg: Config, drive_of, abs_src: str, rel: str) -> str | None:
    """The staging destination for `abs_src`, mirroring internal path.
    Returns None if no staging root is configured (P21/A4: resolves
    drive-letter, UNC, and POSIX-mount-prefix staging keys)."""
    root = staging.root_for(cfg.staging, abs_src, drive_of)
    if not root:
        return None
    return os.path.join(root, rel)


def build_bad_archives(store: Store, cfg: Config,
                       under: list[str] | None = None,
                       drive_of=None) -> PlanResult:
    """Stage archives that fail integrity checks — bad magic bytes or
    unreadable zip table of contents. Content is never decompressed; only
    headers + directory listings are consulted, so a 10 GB corrupt zip
    costs the same as a 10 KB one. Extensions: everything in the
    `Archives` bucket plus `.cbz/.cbr/.cb7`. Staging follows the standard
    `stage_move` op (safeops kernel-only)."""
    inputs = [_require_fresh(store, cfg, "index:library", "mlo scan library")]
    prefixes = [p.replace("/", os.sep).rstrip(os.sep) + os.sep
                for p in (under or [])]

    archive_exts = (set(cfg.taxonomy.get("Archives", ()))
                    | {".cbz", ".cbr", ".cb7"})

    rows: list[dict] = []
    reasons: dict[str, int] = {}
    checked = 0
    for row in store.index_iter():
        rel = row["relpath"]
        if prefixes and not any(
                os.path.normcase(rel).startswith(os.path.normcase(p))
                for p in prefixes):
            continue
        ext = os.path.splitext(rel)[1].lower()
        if ext not in archive_exts:
            continue
        checked += 1
        abs_src = os.path.join(cfg.library_root, rel)
        reason = _check_archive_integrity(abs_src, ext)
        if reason is None:
            continue
        dst = _staging_dest(cfg, drive_of, abs_src, rel)
        if dst is None:
            reasons[f"no-staging:{reason}"] = \
                reasons.get(f"no-staging:{reason}", 0) + 1
            continue
        rows.append(_row("stage_move", abs_src, dst,
                         row["size"], row["quick_hash"], "BAD_ARCHIVE",
                         f"archive:{reason}"))
        reasons[reason] = reasons.get(reason, 0) + 1

    path, plan_id, cap_notes = _register_plan(
        store, cfg, "bad-archives", "library", inputs, rows, drive_of)
    breakdown = ", ".join(f"{r}: {n}" for r, n in sorted(reasons.items())) \
        or "none"
    notes = [f"archives checked: {checked}, bad: {len(rows)}, "
             f"breakdown: {breakdown}"] + cap_notes
    if prefixes:
        notes.append("scoped under: " + ", ".join(under))
    return PlanResult(path, plan_id, len(rows), "bad-archives", "library",
                      notes)


def build_containers(store: Store, cfg: Config,
                     under: list[str] | None = None,
                     drive_of=None) -> PlanResult:
    """Move semantic containers (C33) — phone backups, drive images, app
    backups — into their kind's canonical tree, indexed by IDENTITY (device
    model for phones, container name for the rest), owner disambiguating
    genuine content clashes.

    Design decisions (owner correction #3, 2026-07-11):
      D10 — phone-backup destinations are DEVICE-KEYED:
        `Backups\\Phones\\<S5|S4|Nexus6|...>\\<path-below-device>`. The
        container's OWN name ('Phone Backups', 'CellPhone Backups',
        'User1Backup') is provenance and doesn't survive; the device model
        `containers.find_device()` extracts from the container root OR from
        the first-level child is what indexes the tree. Non-phone kinds keep
        the container-name scheme (drive-image → `Backups\\Drives\\<name>`,
        app-backup → `Backups\\Apps\\<name>`).
      D11 — merge across containers by identity: files from any source that
        target the same identity slot merge into that slot's tree, structure
        intact.
      D12 — collisions resolve at plan time, dedup-first: byte-identical
        collisions dedup (one file survives, the rest are 'already-there'
        skips). Content-different collisions get an OWNER discriminator
        (`Backups\\Phones\\<device>\\<owner>\\<path-below>`), the owner being
        the last non-provenance segment between the bucket and the container
        root. Both sides of the clash are disambiguated (symmetric) so
        neither takes the naive slot when they genuinely differ.
      D7 (unchanged) — protected content inside a container refuses the whole
        plan (L12).

    D5 (unit atomicity) and D6 (twin-with-unit exemption) are SUBSUMED by D12:
    the merge-then-dedup semantics preserve snapshot integrity WITHOUT
    deferring whole containers on collision, and byte-identical files are the
    dedup case, not something to preserve as redundant copies."""
    inputs = [_require_fresh(store, cfg, "index:library", "mlo scan library")]
    prefixes = [p.replace("/", os.sep).rstrip(os.sep) + os.sep
                for p in (under or [])]

    # Index snapshot (fingerprint by normcased relpath so an existing target
    # can be compared to an incoming source without another scan).
    index_fp: dict[str, tuple[int, str]] = {}
    for row in store.index_iter():
        index_fp[os.path.normcase(row["relpath"])] = (
            row["size"], row["quick_hash"])

    # ── Phase 1: match containers, refine kind, compute naive assignments ──
    groups: dict[containers.ContainerMatch, list[tuple[str, dict]]] = {}
    for row in store.index_iter():
        rel = row["relpath"]
        if prefixes and not any(
                os.path.normcase(rel).startswith(os.path.normcase(p))
                for p in prefixes):
            continue
        m = containers.root_of(cfg, rel)
        if m is not None:
            groups.setdefault(m, []).append((rel, row))

    # (rel, row, naive_dst, owner, kind, root, ident_prefix, tail, ident_name)
    # `tail` is what a disambiguator gets inserted BEFORE.
    # `ident_name` is what the row's rule/cluster is keyed on — the DEVICE for
    # phone-backups so files from every source targeting the same device tree
    # cluster together, the container's basename for other kinds.
    assignments: list = []
    kind_counts: dict[str, int] = {}
    already_at_home = 0
    for m in sorted(groups, key=lambda g: os.path.normcase(g.root)):
        members = groups[m]
        # Kind refinement (owner correction #2): a generic '*Backup' whose
        # first-level children name a device joins Backups/Phones — precise
        # detection uses find_device now (was DEVICE_SEG).
        kind, home = m.kind, m.home
        if kind == "app-backup":
            for rel, _r in members:
                below = rel[len(m.root) + 1:]
                first = below.split(os.sep)[0] if below else ""
                if containers.find_device(first) is not None:
                    kind = "phone-backup"
                    home = containers.home_for(cfg, kind) or home
                    break

        owner = containers.owner_of(cfg, m.root)
        # An at-home root (C39: `<home>\<ident>`) has no owner context — the
        # segments between bucket and root ARE the home path, not a person.
        if os.path.normcase(m.root).startswith(
                os.path.normcase(home) + os.sep):
            owner = None
        root_basename = os.path.basename(m.root)
        root_device = containers.find_device(root_basename)   # 'S5 backup' etc.

        for rel, row in members:
            below_root = rel[len(m.root) + 1:]
            below_segs = below_root.split(os.sep) if below_root else []

            if kind == "phone-backup":
                if root_device is not None:
                    # Container root itself IS the device.
                    device, tail = root_device, below_root
                elif below_segs and \
                        containers.find_device(below_segs[0]) is not None:
                    # First-level child is the device.
                    device = containers.find_device(below_segs[0])
                    tail = os.sep.join(below_segs[1:])
                else:
                    # No device — land in <home>\Unsorted\ (the container's
                    # own name is provenance too and does NOT survive; the
                    # human sorts the Unsorted pile). Naming matches the
                    # taxonomy's Photos/Music/Video 'Unsorted' shelves.
                    device = "Unsorted"
                    tail = below_root
                naive_dst = os.sep.join([home, device, tail]) if tail \
                    else os.sep.join([home, device])
                identity_prefix = os.sep.join([home, device])
                ident_name = device
            else:
                # app-backup / drive-image: container name indexes the tree.
                identity_prefix = os.sep.join([home, root_basename])
                naive_dst = os.sep.join([identity_prefix, below_root]) \
                    if below_root else identity_prefix
                tail = below_root
                ident_name = root_basename

            if os.path.normcase(naive_dst) == os.path.normcase(rel):
                already_at_home += 1
                continue

            assignments.append(
                (rel, row, naive_dst, owner, kind, m.root, identity_prefix,
                 tail, ident_name))
        kind_counts[kind] = kind_counts.get(kind, 0) + 1

    # ── Phase 2: collision resolution (dedup-first, then owner-disambiguate) ──
    rows: list[dict] = []
    used_dsts: set[str] = set()
    dedup_skipped = disambiguated = clash_deferred = 0
    clash_notes: list[str] = []

    by_naive: dict[str, list[int]] = {}
    for i, a in enumerate(assignments):
        by_naive.setdefault(os.path.normcase(a[2]), []).append(i)

    for naive_key in sorted(by_naive):
        idxs = by_naive[naive_key]
        # Partition incoming by fingerprint (dedup collisions are same-fp).
        by_fp: dict[tuple[int, str], list[int]] = {}
        for i in idxs:
            fp = (assignments[i][1]["size"], assignments[i][1]["quick_hash"])
            by_fp.setdefault(fp, []).append(i)

        # Is an existing library file already at the target?
        existing_fp = index_fp.get(naive_key)

        # Reduce each fp class to ONE representative; count the rest as dedup.
        # An fp class matching an existing target's fp is entirely 'already there'.
        reps: list[int] = []
        for fp, group in by_fp.items():
            if fp == existing_fp:
                dedup_skipped += len(group)          # all in this class dedup
            else:
                dedup_skipped += len(group) - 1
                reps.append(group[0])

        if not reps:
            continue

        needs_disamb = (existing_fp is not None) or len(reps) >= 2
        for i in reps:
            rel, row, naive_dst, owner, kind, root, ident_prefix, tail, \
                ident_name = assignments[i]
            if not needs_disamb:
                dest = naive_dst
            else:
                # D12: insert owner between identity and tail. When there's
                # no owner (root sits directly under the bucket), fall back
                # to the container's basename so a clash is at least
                # discriminable — it's provenance, but under DEMONSTRATED
                # collision so the user sees why.
                disc = owner or os.path.basename(root)
                dest = os.sep.join([ident_prefix, disc, tail]) if tail \
                    else os.sep.join([ident_prefix, disc])
                disambiguated += 1

            dest_key = os.path.normcase(dest)
            if index_fp.get(dest_key) == (row["size"], row["quick_hash"]):
                # Byte-identical occupant at the (possibly disambiguated)
                # dest: the naive-path dedup rule applies here too —
                # emitting a row would drift forever on 'destination
                # occupied' for content that is already home.
                dedup_skipped += 1
                continue
            if dest_key in used_dsts or dest_key in index_fp:
                # A double-clash (disambiguator itself is taken). Rare; log
                # and skip this row — the whole plan doesn't fail.
                clash_deferred += 1
                clash_notes.append(f"double-clash (unresolved): {rel} -> {dest}")
                continue
            used_dsts.add(dest_key)
            rows.append(_row(
                "move_within",
                os.path.join(cfg.library_root, rel),
                os.path.join(cfg.library_root, dest),
                row["size"], row["quick_hash"], "CONTAINER",
                f"container:{kind}:{ident_name}"))

    plan_path, plan_id, cap_notes = _register_plan(
        store, cfg, "containers", "library", inputs, rows, drive_of)
    kinds = ", ".join(f"{k}: {n}" for k, n in sorted(kind_counts.items())) \
        or "none"
    notes = [f"containers processed: {len(groups)} ({kinds}), "
             f"files planned: {len(rows)}, already at home: {already_at_home}, "
             f"dedup skipped (byte-identical collisions): {dedup_skipped}, "
             f"disambiguated by owner (content-different clashes): "
             f"{disambiguated}, double-clash deferred: {clash_deferred}"] + cap_notes
    notes.extend(clash_notes[:5])
    if len(clash_notes) > 5:
        notes.append(f"(+{len(clash_notes)-5} more clash notes …)")
    if prefixes:
        notes.append("scoped under: " + ", ".join(under))
    return PlanResult(plan_path, plan_id, len(rows), "containers", "library",
                      notes)


def build_flatten_provenance(store: Store, cfg: Config,
                             under: list[str] | None = None,
                             exclude_srcs: set[str] | None = None,
                             drive_of=None) -> PlanResult:
    """Strip device-origin path segments (E_NAS1, G_Phone1, HDD2_Part2, …)
    from indexed files. The user's core complaint (C27): reorganize can't touch
    these — non-media buckets return None from taxonomy.route(), Audio hits are
    C19-blocked — so provenance folder names survive forever.

    Narrow by design:
      - EVERY intermediate segment (1..n-1) is checked against
        taxonomy._PROVENANCE_SEG and all matches strip in one pass (C34 —
        the original seg-1-only rule left `Other\\I_SSD1\\User2 S8
        backup\\…` half-flattened); segment 0 (the bucket) and the filename
        are never stripped;
      - **media-bucket tops are skipped at the DIRECT CHILD only** (C28,
        narrowed by C47): a provenance segment sitting right under
        Audio/Video/Videos/Photos/Images (`Audio\\I_SSD1\\x.mp3`) is
        still untouched — it is the signal that audio/photo-triage hasn't
        caught up yet, and papering over it would launder an unidentified
        file into the top of a curated bucket. But a provenance segment
        DEEPER inside one of the curated layout roots
        (`music_root\\<genre>\\<PROV>\\…`, `photos_root\\<year>\\<PROV>\\…`)
        IS stripped (C47): the file is already inside a curated sub-tree —
        already triaged — and the device folder is a proven interloper with
        an unambiguous de-provenanced parent. `personal_root` is
        DELIBERATELY EXCLUDED from this deeper-strip set (2026-07-15 fix):
        personal videos are date-drain / Undated-shelf territory (C45), not
        flatten's — a device folder under Video\\Personal is either dated by
        the drain or dropped to `personal_root\\Undated\\`, never stripped
        in place by flatten;
      - exclude_srcs prevents same-src double-planning across a pilot run
        (date-drain already claims Photos\\E_NAS1\\setup.bmp today);
      - junk (classify_junk) is left for the dedup/staging path — never re-homed;
      - fingerprint twins anywhere in the library are dedup decisions, not
        placement decisions (C21) — they stay put;
      - dest occupied in the normcased index, or two candidates colliding on the
        same dest, are skipped-and-reported — never resolved by naming (L1/L17)."""
    inputs = [_require_fresh(store, cfg, "index:library", "mlo scan library")]
    prefixes = [p.replace("/", os.sep).rstrip(os.sep) + os.sep
                for p in (under or [])]
    excludes = {os.path.normcase(s) for s in (exclude_srcs or ())}
    media_tops = {lbl.casefold() for lbl in taxonomy.MEDIA_LABELS}

    index_normcased: set[str] = set()
    fp_count: dict[tuple[int, str], int] = {}
    candidates: list[tuple[str, dict, str, str]] = []  # (rel, row, new_rel, seg1)
    out_of_scope = excluded = junked = no_seg = media_skip = container_skip = 0
    for row in store.index_iter():
        rel = row["relpath"]
        index_normcased.add(os.path.normcase(rel))
        fp_count[(row["size"], row["quick_hash"])] = \
            fp_count.get((row["size"], row["quick_hash"]), 0) + 1
        if prefixes and not any(
                os.path.normcase(rel).startswith(os.path.normcase(p))
                for p in prefixes):
            out_of_scope += 1
            continue
        src_abs = os.path.join(cfg.library_root, rel)
        if os.path.normcase(src_abs) in excludes:
            excluded += 1
            continue
        if taxonomy.classify_junk(cfg, rel, row["size"]) is not None:
            junked += 1
            continue
        segs = rel.replace("/", os.sep).split(os.sep)
        if len(segs) < 3:
            no_seg += 1
            continue
        # Container members are a UNIT (C33) — flatten never strips a segment
        # that is, or is inside, a declared container; build_containers owns
        # that subtree (a backup-named wrapper is a snapshot, not a dump).
        if containers.root_of(cfg, rel) is not None:
            container_skip += 1
            continue
        # C47: a media-top path is only fully out-of-scope when it is NOT
        # inside one of the curated layout roots (music/tv/movies/photos) —
        # the C28 boundary. When it IS inside a curated root, the root's own
        # segments (bucket + everything through the root path, e.g.
        # `Audio\Music`) are frozen — never stripped — but a provenance
        # segment deeper than the root (the genre/year subfolder onward) is
        # fair game, same as any other bucket. `personal_root` is
        # DELIBERATELY OMITTED here (2026-07-15 fix): personal videos are
        # C45 date-drain / Undated-shelf territory, never flatten's — see
        # this function's docstring.
        rel_posix = rel.replace(os.sep, "/")
        is_media_top = segs[0].casefold() in media_tops
        strip_from = 1
        if is_media_top:
            lay = cfg.layout
            curated_roots = (lay.music_root, lay.tv_root, lay.movies_root,
                             lay.photos_root)
            root_depth = None
            for root in curated_roots:
                if taxonomy._inside(rel_posix, root) is not None:
                    root_depth = len(root.replace("\\", "/").strip("/")
                                     .split("/"))
                    break
            if root_depth is None:
                # A media-top path OUTSIDE every curated root — the original
                # C28 dump case (unidentified media sitting right at the
                # bucket top). Untouched, in full.
                media_skip += 1
                continue
            strip_from = root_depth
        # C34 (nested-segment flatten): strip EVERY intermediate segment at
        # or past `strip_from` that matches _PROVENANCE_SEG. Segments before
        # strip_from (the bucket for a non-media path, or the whole curated
        # root path for a media-top path) are frozen — never stripped. Bucket
        # (seg 0) and filename (seg -1) are never stripped either way.
        stripped_segs = list(segs[1:strip_from]) + [
            s for s in segs[strip_from:-1]
            if not taxonomy._PROVENANCE_SEG.search(s)]
        n_dropped = (len(segs) - 2) - len(stripped_segs)
        if n_dropped == 0:
            if is_media_top:
                media_skip += 1
            else:
                no_seg += 1
            continue
        # Rule tag: the outermost provenance segment actually stripped.
        first_prov = next(s for s in segs[strip_from:-1]
                          if taxonomy._PROVENANCE_SEG.search(s))
        new_rel = os.sep.join([segs[0]] + stripped_segs + [segs[-1]])
        candidates.append((rel, row, new_rel, first_prov))

    # C21 dedup guard — files with a fingerprint twin ANYWHERE are dedup
    # decisions, not placement decisions. Leave them for dedup-library.
    kept: list[tuple[str, dict, str, str]] = []
    duplicated = 0
    for rel, row, new_rel, seg1 in candidates:
        if fp_count.get((row["size"], row["quick_hash"]), 0) > 1:
            duplicated += 1
        else:
            kept.append((rel, row, new_rel, seg1))
    candidates = kept

    # L1/L17 collision handling: normcased dest occupancy against the index AND
    # candidate-to-candidate collisions. Skip-and-report; never rename.
    dest_count: dict[str, int] = {}
    for _, _, new_rel, _ in candidates:
        key = os.path.normcase(new_rel)
        dest_count[key] = dest_count.get(key, 0) + 1
    collided = 0
    rows: list[dict] = []
    for rel, row, new_rel, seg1 in candidates:
        key = os.path.normcase(new_rel)
        if dest_count[key] > 1 or key in index_normcased:
            collided += 1
            continue
        rows.append(_row("move_within",
                         os.path.join(cfg.library_root, rel),
                         os.path.join(cfg.library_root, new_rel),
                         row["size"], row["quick_hash"], "FLATTEN",
                         f"flatten:provenance:{seg1}"))

    plan_path, plan_id, cap_notes = _register_plan(
        store, cfg, "flatten-provenance", "library", inputs, rows, drive_of)
    notes = [f"moves: {len(rows)}, excluded (claimed by prior sections): "
             f"{excluded}, junk (stay put): {junked}, media bucket (stay put): "
             f"{media_skip}, container member (C33, stay put): "
             f"{container_skip}, no provenance segment: {no_seg}, "
             f"duplicate content (stay put): {duplicated}, "
             f"collisions (stay put): {collided}"] + cap_notes
    if prefixes:
        notes.append("scoped under: " + ", ".join(under))
    return PlanResult(plan_path, plan_id, len(rows), "flatten-provenance",
                      "library", notes)


def _media_exts_for(cfg: Config) -> set[str]:
    exts: set[str] = set()
    for label in ("Video", "Videos", "Audio", "Photos", "Images"):
        exts.update(cfg.taxonomy.get(label, ()))
    return exts


def _confirm_twin(cfg: Config, src: str, twin_relpaths: list[str]) -> bool:
    """True iff `src` is a genuine duplicate of at least one of its candidate
    library twins — the escalated identity check before an ORGANIZED original
    is staged OUT. Delegates to fingerprint.confirm_duplicate (P21/A3): the
    verdict matched on the 128 KiB quick screen; this re-confirms with a full
    SHA-256 for any file over 256 KiB, so a same-size/same-ends/different-
    middle file is never mistaken for a duplicate and swept off its only
    unique content — regardless of file size."""
    from . import fingerprint
    for rel in twin_relpaths:
        if fingerprint.confirm_duplicate(
                src, os.path.join(cfg.library_root, rel)):
            return True
    return False


def build_dedup(store: Store, cfg: Config, source_name: str,
                waive_organize: bool = False,
                drive_of=None, confirm_bytes: int = 0) -> PlanResult:
    """ORGANIZED + JUNK files -> that drive's staging root. Same-drive by
    construction; refuses to run before the organize plan unless waived (L13).
    drive_of is injectable for tests (fake drives on one tmp filesystem).

    confirm_bytes > 0 re-confirms every ORGANIZED row against its library twin
    before staging (fingerprint.confirm_duplicate — quick match + full SHA-256
    above 256 KiB, P21/A3); a row that fails is kept in place and counted,
    never staged. The numeric value only gates on/off; the confirmation policy
    itself is fixed."""
    src_cfg = cfg.source(source_name)
    inputs = _fresh_inputs(store, cfg, source_name)

    n_unique = sum(1 for _ in store.source_iter(source_name, "UNIQUE"))
    if n_unique and not waive_organize:
        # An organize plan that completed via a residual carries kind
        # 'organize-residual'; it still satisfies copy-before-stage (C9).
        organized_executed = any(
            a.kind == "plan" and a.status == "executed"
            and a.scope.get("kind", "").split("-residual")[0] == "organize"
            and a.scope.get("source") == source_name
            for a in store.artifacts_all())
        if not organized_executed:
            raise OrderingError(
                f"source '{source_name}' still has {n_unique} UNIQUE files that no "
                f"executed organize plan covers (L13: copy before stage). Run "
                f"`mlo plan organize {source_name}` + apply it first, or pass "
                f"--waive-organize to stage anyway.")

    if drive_of is None:
        from . import winpath
        drive_of = winpath.drive_of
    staging_root = staging.root_for(cfg.staging, src_cfg.root, drive_of)
    if not staging_root:
        raise PlanError(
            f"no [staging] root configured for "
            f"{drive_of(src_cfg.root)!r} (source '{source_name}')")

    rows = []
    confirm_failed = 0
    for verdict in ("ORGANIZED", "JUNK"):
        for r in store.source_iter(source_name, verdict):
            src = os.path.join(src_cfg.root, r["relpath"])
            if verdict == "ORGANIZED" and confirm_bytes:
                twins = store.index_lookup(r["size"], r["quick_hash"])
                if not _confirm_twin(cfg, src, twins):
                    confirm_failed += 1        # unproven twin -> stays put
                    continue
            dst = os.path.join(staging_root, source_name, r["relpath"])
            rows.append(_row("stage_move", src, dst, r["size"], r["quick_hash"],
                             verdict, r["verdict_rule"] or ""))

    path, plan_id, cap_notes = _register_plan(
        store, cfg, "dedup", source_name, inputs, rows, drive_of)
    notes = list(cap_notes)
    if confirm_bytes:
        notes.append(
            "confirmed ORGANIZED twins (quick match, full SHA-256 above "
            f"256KiB — P21/A3); {confirm_failed} failed confirm "
            "(kept in place, not staged)")
    if n_unique and waive_organize:
        notes.append(f"WAIVED: {n_unique} UNIQUE files remain unorganized (L13)")
    return PlanResult(path, plan_id, len(rows), "dedup", source_name, notes,
                      confirm_failed=confirm_failed)


def _library_staging_root(cfg: Config, drive_of) -> str:
    if drive_of is None:
        from . import winpath
        drive_of = winpath.drive_of
    root = staging.root_for(cfg.staging, cfg.library_root, drive_of)
    if not root:
        raise PlanError(
            f"no [staging] root configured for the library's "
            f"{drive_of(cfg.library_root)!r}")
    return root


def _inode_of(cfg: Config, rel: str) -> tuple[int, int] | None:
    """(st_dev, st_ino) for a library-relative path, or None when unreadable
    OR when st_nlink <= 1 (no other hardlink exists — the common case, fast
    filter). P21/A5: two hardlinks to one inode are byte-identical by
    definition and would otherwise be double-counted as reclaimable-bytes
    duplicates — decisive on NAS backup shares (rsnapshot/rsync --link-dest/
    Time-Machine-style snapshots), which create entire hardlink farms."""
    try:
        st = os.stat(winpath.to_long(os.path.join(cfg.library_root, rel)))
    except OSError:
        return None
    if st.st_nlink <= 1:
        return None
    return (st.st_dev, st.st_ino)


def build_dedup_library(store: Store, cfg: Config,
                        under: list[str] | None = None,
                        drive_of=None) -> PlanResult:
    """Stage byte-identical duplicate content OUT of the library — C21's
    counterpart: reorganize leaves fingerprint twins in place; this is the
    dedup decision, made destructively-safe. Contract:

      - quick fingerprints NOMINATE groups; FULL SHA-256 over every member
        CONFIRMS them — a group with any mismatch or unreadable member is
        skipped whole and reported (a quick-fp collision is a loud note,
        never a staged file);
      - one canonical copy always stays: a copy outside the `under` scopes
        (curated trees) wins; else the normcase-lexicographically first;
      - extras stage to <staging>/dedup/<relpath> (same drive, journaled,
        reversible); the kernel deletes their index rows transactionally;
      - zero-byte files are junk territory (triage), never dedup rows.
    """
    inputs = [_require_fresh(store, cfg, "index:library", "mlo scan library")]
    staging_root = _library_staging_root(cfg, drive_of)
    prefixes = [os.path.normcase(p.replace("/", os.sep).rstrip(os.sep) + os.sep)
                for p in (under or [])]
    # The layout roots are always canonical territory (C22): the router never
    # restructures them, and dedup must never stage OUT of them — even when an
    # operator's --under scope covers them (`--under Audio` includes
    # Audio/Music). A curated copy can only ever be the copy that STAYS.
    lay = cfg.layout
    curated = [os.path.normcase(r.replace("/", os.sep).rstrip(os.sep) + os.sep)
               for r in (lay.movies_root, lay.tv_root, lay.music_root,
                         lay.photos_root, lay.personal_root)]

    def in_scope(rel: str) -> bool:
        n = os.path.normcase(rel)
        if any(n.startswith(c) for c in curated):
            return False
        # Container members are snapshot content (D6/C39): dedup must never
        # cherry-pick a file out of a container — the copy inside the unit is
        # part of its integrity. Like curated trees, a container copy can
        # only ever be the copy that STAYS (canonical-preferred).
        if containers.root_of(cfg, rel) is not None:
            return False
        return not prefixes or any(n.startswith(p) for p in prefixes)

    groups: dict[tuple[int, str], list[dict]] = {}
    for row in store.index_iter():
        if row["size"] == 0:
            continue
        groups.setdefault((row["size"], row["quick_hash"]), []).append(row)

    rows, kept = [], 0
    skipped_unreadable = fp_collisions = 0
    hashed_bytes = 0
    hardlinked_bytes = hardlinked_files = 0
    for (size, _), members in sorted(groups.items(),
                                     key=lambda kv: kv[1][0]["relpath"]):
        if len(members) < 2:
            continue
        scoped = [m for m in members if in_scope(m["relpath"])]
        if not scoped:
            continue
        outside = [m for m in members if not in_scope(m["relpath"])]
        if outside:
            canonical = min(outside, key=lambda m: os.path.normcase(m["relpath"]))
            extras = scoped
        else:
            canonical = min(scoped, key=lambda m: os.path.normcase(m["relpath"]))
            extras = [m for m in scoped if m is not canonical]
        if not extras:
            continue
        try:
            digests = {m["relpath"]: fingerprint.full(
                os.path.join(cfg.library_root, m["relpath"]))
                for m in members}
            hashed_bytes += size * len(members)
        except OSError:
            skipped_unreadable += 1
            continue
        if len(set(digests.values())) != 1:
            fp_collisions += 1        # quick-fp equal, bytes differ: stay put
            continue
        kept += 1
        # P21/A5: any extra sharing an inode with an already-represented copy
        # (the canonical, or an earlier extra) is a HARDLINK, not separate
        # storage — staging it would double-count reclaimable bytes and, if
        # the user later deletes the staged copy, free nothing (the inode's
        # last remaining link keeps the disk blocks alive). Stage at most one
        # representative per distinct inode.
        seen_inodes: set[tuple[int, int]] = set()
        canonical_inode = _inode_of(cfg, canonical["relpath"])
        if canonical_inode is not None:
            seen_inodes.add(canonical_inode)
        for m in extras:
            ino = _inode_of(cfg, m["relpath"])
            if ino is not None and ino in seen_inodes:
                hardlinked_files += 1
                hardlinked_bytes += m["size"]
                continue
            if ino is not None:
                seen_inodes.add(ino)
            rows.append(_row(
                "stage_move",
                os.path.join(cfg.library_root, m["relpath"]),
                os.path.join(staging_root, "dedup", m["relpath"]),
                m["size"], m["quick_hash"], "DUPLICATE",
                f"dup:keep:{canonical['relpath']}"))

    path, plan_id, cap_notes = _register_plan(
        store, cfg, "dedup-library", "library", inputs, rows, drive_of)
    notes = [f"confirmed groups: {kept}, staged extras: {len(rows)}, "
             f"full-hashed: {hashed_bytes / 2**30:.2f} GiB, "
             f"unreadable groups (stay put): {skipped_unreadable}, "
             f"quick-fp collisions (stay put): {fp_collisions}"] + cap_notes
    if hardlinked_files:
        notes.append(
            f"P21/A5: {hardlinked_files} file(s) ({hardlinked_bytes / 2**20:.1f} "
            f"MiB) were hardlinks to an already-represented copy — never "
            f"staged (staging them would not reclaim any space)")
    if fp_collisions:
        notes.append(f"NOTE: {fp_collisions} group(s) matched quick fingerprints "
                     f"but differ byte-for-byte — worth a look")
    if prefixes:
        notes.append("scoped under: " + ", ".join(under))
    return PlanResult(path, plan_id, len(rows), "dedup-library", "library", notes)


def build_stage_library(store: Store, cfg: Config, relpaths: list[str],
                        label: str = "triage",
                        drive_of=None) -> PlanResult:
    """Stage an explicit list of library files (e.g. triage-judged junk) to
    <staging>/<label>/<relpath>. Every path must be a current index row —
    a stale or mistyped path refuses the whole plan rather than partially
    staging. Same-drive, journaled, reversible; disposal stays human."""
    inputs = [_require_fresh(store, cfg, "index:library", "mlo scan library")]
    staging_root = _library_staging_root(cfg, drive_of)
    index = {os.path.normcase(r["relpath"]): r for r in store.index_iter()}

    rows, missing = [], []
    for rel in relpaths:
        rel = rel.replace("/", os.sep)
        row = index.get(os.path.normcase(rel))
        if row is None:
            missing.append(rel)
            continue
        rows.append(_row(
            "stage_move",
            os.path.join(cfg.library_root, row["relpath"]),
            os.path.join(staging_root, label, row["relpath"]),
            row["size"], row["quick_hash"], "TRIAGE", f"stage:{label}"))
    if missing:
        raise PlanError(
            f"{len(missing)} path(s) are not in the library index — refusing "
            f"to build (rescan or fix the list): " + "; ".join(missing[:5])
            + (" …" if len(missing) > 5 else ""))

    path, plan_id, cap_notes = _register_plan(
        store, cfg, "stage-library", "library", inputs, rows, drive_of)
    return PlanResult(path, plan_id, len(rows), "stage-library", "library",
                      [f"staging to: {os.path.join(staging_root, label)}"]
                      + cap_notes)


def build_dispose(store: Store, cfg: Config, staging_key: str | None = None,
                  drive_of=None) -> PlanResult:
    """P21/C2 — the L18 amendment: every ORDINARY file currently sitting in
    the configured staging root(s) -> a dispose plan (Windows Recycle Bin /
    POSIX XDG trash). Only files the JOURNAL recognizes as this engine's own
    staged output are included — content the journal can't explain (someone
    dropped it into staging by hand, or an anomaly `mlo verify staging`
    exists to catch) is excluded and reported, never disposed blind. Live
    protected content in staging still hard-refuses the WHOLE build via
    `_register_plan`'s existing L12 check (`_reject_protected`) — the same
    contract every other builder already has, not something this builder
    special-cases."""
    from . import verify as verifymod
    if staging_key is not None and staging_key not in cfg.staging:
        raise PlanError(f"unknown staging key: {staging_key!r} (configured: "
                        f"{', '.join(sorted(cfg.staging)) or 'none'})")
    roots = ({staging_key: cfg.staging[staging_key]} if staging_key
             else dict(cfg.staging))
    # Path AND content must both be journal-explained: membership by
    # normpath+normcase (a config-authored staging root and a live os.walk
    # path are cosmetically different strings for the same file), and the
    # file's CURRENT fingerprint must match the one the journal recorded
    # when the engine staged it — a path the journal knows whose bytes have
    # since been replaced is disposed by neither.
    journaled = store.staged_dst_fingerprints()

    rows: list[dict] = []
    skipped_unjournaled = skipped_content_drift = skipped_unreadable = 0
    for _key, root in sorted(roots.items()):
        if not os.path.isdir(winpath.to_long(root)):
            continue
        for lpath in verifymod._walk_files_including_protected(root, ()):
            plain = winpath.from_long(lpath)
            if plain.endswith(".mlopart"):
                continue    # a failed copy's inert residue — verify's job
            pre = journaled.get(os.path.normpath(os.path.normcase(plain)))
            if pre is None:
                skipped_unjournaled += 1
                continue
            try:
                size, qh = fingerprint.quick(plain)
            except OSError:
                skipped_unreadable += 1
                continue
            if pre[0] is None or pre[1] is None or (size, qh) != pre:
                skipped_content_drift += 1
                continue
            rows.append(_row("dispose", plain, plain, size, qh,
                             "DISPOSE", "dispose:staging"))

    source_name = staging_key or "all"
    plan_path, plan_id, cap_notes = _register_plan(
        store, cfg, "dispose", source_name, [], rows, drive_of)
    notes = [f"files to dispose: {len(rows)}"] + cap_notes
    if skipped_unjournaled:
        notes.append(f"{skipped_unjournaled} file(s) in staging the journal "
                     f"can't explain — not disposed (run `mlo verify staging` "
                     f"to investigate)")
    if skipped_content_drift:
        notes.append(f"{skipped_content_drift} file(s) at a journaled staging "
                     f"path no longer match the content the engine staged "
                     f"there — not disposed")
    if skipped_unreadable:
        notes.append(f"{skipped_unreadable} unreadable file(s) in staging — "
                     f"not disposed")
    return PlanResult(plan_path, plan_id, len(rows), "dispose", source_name, notes)
