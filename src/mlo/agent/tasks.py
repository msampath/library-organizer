"""The three bounded tasks (docs/agent-design.md §4): classify, triage, orchestrate.

Pre-digestion is deterministic and happens HERE, not in the model: extension
histograms, folder rollups, exemplars — every prompt fits a small context
regardless of library size. Option spaces are enumerated from config/engine
state; the model can only pick, never invent.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass

from ..config import Config
from ..store import Store
from .llm import ChainClient
from .protocol import (TaskSpec, UNSURE, majority_vote, require_choice,
                       require_confidence, require_keys, run_task, SchemaError)

CONFIDENCE_FLOOR = 0.7          # below this a local answer gets a 3-vote
DANGEROUS_GUARD = 0.95          # stage-junk over personal media needs this much


# ── classify: label the unmatched tail ──────────────────────────────────────

def _classify_spec(labels: tuple[str, ...]) -> TaskSpec:
    options = ", ".join(labels)

    def validate(obj: dict) -> dict:
        require_keys(obj, ("items",))
        if not isinstance(obj["items"], list):
            raise SchemaError("items must be a list")
        for it in obj["items"]:
            if not isinstance(it, dict):
                # a bare string/number item must trigger the repair/abstain
                # ladder as a SchemaError, not crash the run with TypeError
                raise SchemaError("each item must be a JSON object")
            require_keys(it, ("i", "label", "confidence"))
            require_choice(it["label"], set(labels) | {UNSURE}, "label")
            require_confidence(it)
            if not isinstance(it["i"], int) or isinstance(it["i"], bool):
                raise SchemaError("item index i must be an integer")
        return obj

    return TaskSpec(
        name="classify",
        system=(
            "You label file paths into a fixed taxonomy for a personal media "
            f"library. Allowed labels: {options}, or {UNSURE}. Judge ONLY from "
            "the path (folder names, filename, extension). If a path could "
            f"plausibly fit two labels, or fits none, answer {UNSURE} — an "
            "honest abstention beats a guess. Reply with ONLY a JSON object: "
            '{"items": [{"i": <index>, "label": "<label>", '
            '"confidence": <0..1>}, ...]} covering every input index.'),
        validate=validate)


def classify_unmatched(client: ChainClient, store: Store, cfg: Config,
                       source_name: str, batch_size: int = 20,
                       limit: int | None = None) -> dict:
    """Propose labels for REVIEW files. Proposals only — nothing moves; the
    output is evidence for a human (or for new [taxonomy.buckets] rules)."""
    labels = tuple(cfg.taxonomy.keys())
    spec = _classify_spec(labels)
    rows = [r["relpath"] for r in store.source_iter(source_name, "REVIEW")]
    if limit:
        rows = rows[:limit]
    proposals, unsure = [], []
    for start in range(0, len(rows), batch_size):
        chunk = rows[start:start + batch_size]
        user = "Label these paths:\n" + "\n".join(
            f"{i}: {p}" for i, p in enumerate(chunk))
        out = run_task(client, spec, user)
        if out.value is None:
            unsure.extend(chunk)
            continue
        got = {it["i"]: it for it in out.value["items"] if 0 <= it["i"] < len(chunk)}
        for i, path in enumerate(chunk):
            it = got.get(i)
            if it is None or it["label"] == UNSURE:
                unsure.append(path)
            elif it["confidence"] < CONFIDENCE_FLOOR:
                # Self-consistency buys accuracy back where tokens are FREE
                # (protocol.majority_vote's stated design); on a cloud-only
                # chain a 3-vote is 3x paid spend per marginal item — the
                # low-confidence item joins the human queue instead.
                if not client.has_local():
                    unsure.append(path)
                    continue
                voted = majority_vote(
                    client, spec, f"Label these paths:\n0: {path}",
                    key=lambda v: v["items"][0]["label"] if v["items"] else UNSURE)
                if voted.value and voted.value["items"] \
                        and voted.value["items"][0]["label"] != UNSURE:
                    proposals.append({"relpath": path,
                                      "label": voted.value["items"][0]["label"],
                                      "confidence": it["confidence"],
                                      "via": "self-consistency"})
                else:
                    unsure.append(path)
            else:
                proposals.append({"relpath": path, "label": it["label"],
                                  "confidence": it["confidence"], "via": "direct"})
    return {"proposals": proposals, "unsure": unsure,
            "total": len(rows)}


# ── classify v2: media identity for the router (v0.2) ───────────────────────

MEDIA_KINDS = ("movie", "tv", "personal", "music")
JUNK = "junk"      # classify-only verdict: never becomes a hint, stays put

# Distilled name patterns (docs/classification-patterns.md): definitional
# recorder/cache conventions learned from a real 388K-file library and
# frontier-verified (2026-07-06). These decide BEFORE any model call — the
# LLM only ever sees the tail no pattern catches. First match wins; config
# [classify.name_patterns] entries are consulted before these defaults.
NAME_PATTERNS: tuple[tuple[str, str, str], ...] = (
    # (rule id, kind, regex against the basename)
    ("whatsapp-media",   "personal", r"^VID-\d{8}-WA\d"),
    ("phone-timestamp",  "personal", r"^(19|20)\d{6}[_-]\d{6}"),
    ("dashcam-stamp",    "personal", r"^\d{14}_\d+[AB]?\."),
    ("dashcam-file",     "personal", r"^FILE\d{4,}"),
    ("camera-prefix",    "personal", r"^(VID|MOV|MVI|IMG)[_-]?\d"),
    ("nikon-dsc",        "personal", r"^DSC[_-]?\d"),
    ("kodak-numbered",   "personal", r"^\d{3}_\d{4}\."),
    ("screen-recording", "personal", r"^Screen_Recording_\d"),
    ("ios-export",       "personal", r"^\d{9,}__[0-9A-Fa-f-]{30,}"),
    ("ad-network-cache", JUNK,       r"^UnityAds-"),
    ("web-video-cache",  JUNK,       r"^\d{9,10}_\d{2,4}x\d{2,4}_"),
    ("hex-named-cache",  JUNK,       r"^[0-9a-f]{32,40}[._-]"),
    ("temp-partial",     JUNK,       r"^\.temp-"),
)


def match_name_pattern(cfg: Config, filename: str) -> tuple[str, str] | None:
    """(kind, rule_id) from the first matching name pattern, else None.
    Config patterns win over the built-in defaults."""
    for kind, regexes in cfg.name_patterns.items():
        for rx in regexes:
            if re.match(rx, filename, re.IGNORECASE):
                return kind, f"pattern:config:{kind}"
    for rule, kind, rx in NAME_PATTERNS:
        if re.match(rx, filename, re.IGNORECASE):
            return kind, f"pattern:{rule}"
    return None


def _media_spec(languages: tuple[str, ...]) -> TaskSpec:
    kinds = ", ".join(MEDIA_KINDS + (JUNK,))
    langs = ", ".join(languages)

    def validate(obj: dict) -> dict:
        require_keys(obj, ("items",))
        if not isinstance(obj["items"], list):
            raise SchemaError("items must be a list")
        for it in obj["items"]:
            if not isinstance(it, dict):
                raise SchemaError("each item must be an object")
            require_keys(it, ("i", "media_kind", "language", "year", "confidence"))
            if not isinstance(it["i"], int) or isinstance(it["i"], bool):
                raise SchemaError("item index i must be an integer")
            require_choice(it["media_kind"], set(MEDIA_KINDS) | {JUNK, UNSURE},
                           "media_kind")
            require_choice(it["language"], set(languages) | {UNSURE}, "language")
            y = it["year"]
            if y is not None and (isinstance(y, bool) or not isinstance(y, int)
                                  or not 1900 <= y <= 2035):
                raise SchemaError("year must be null or an integer 1900-2035")
            require_confidence(it)
        return obj

    return TaskSpec(
        name="classify-media",
        system=(
            "You identify media files for a personal library, judging ONLY "
            "from each path (folders, filename, extension). For every item "
            f"give: media_kind — one of {kinds}, or {UNSURE}; language — one "
            f"of {langs}, or {UNSURE}; year — the release year as an integer "
            "1900-2035, or null when the path does not state one. Movies "
            "usually carry a title and often a year; TV has SxxEyy or season "
            "markers; camera, dashcam, WhatsApp, screenshot and "
            "screen-recording patterns are 'personal'. Distilled judgment "
            "from real libraries: device-vendor promo/tutorial videos "
            "(drive-maker marketing), ad-network caches, epoch- or hex-named "
            "web video caches, .temp partial downloads and game screen "
            f"captures are '{JUNK}' — junk never moves, so a wrong junk call "
            "is recoverable. Old FLV/site-rip song clips are 'music'. School "
            "events, kids' recitals and named home recordings are 'personal'. "
            "DVD VTS_*.VOB files take their identity from their folder — "
            "classify them but give year=null. Never invent a year or "
            f"language; when torn, answer {UNSURE} — an honest abstention "
            "beats a guess. Reply with ONLY a JSON object: "
            '{"items": [{"i": <index>, "media_kind": "<kind>", '
            '"language": "<language>", "year": <int or null>, '
            '"confidence": <0..1>}, ...]} covering every input index.'),
        validate=validate)


def classify_media(client: ChainClient, store: Store, cfg: Config,
                   source_or_none: str | None, relpaths: list[str],
                   batch_size: int = 15) -> dict:
    """Media identity (kind / language / year) for the router's UNMATCHED tail
    (taxonomy.route() returned None). Hints only — nothing moves; the caller
    feeds the result to plan organize/reorganize, where every placement still
    passes the plan gates.

    Deterministic pre-pass first: distilled name patterns (NAME_PATTERNS +
    [classify.name_patterns] config) decide the definitional cases — recorder
    conventions become hints, cache/vendor-debris conventions become 'junk' —
    without an LLM call. Only the remaining tail goes to the model, which may
    also answer 'junk'. Junk NEVER becomes a hint (the file stays put); it is
    returned separately for the human/triage report. media_kind=UNSURE drops
    the whole item to 'unsure' (kind is load-bearing); language=UNSURE keeps
    the item with language=None so route() falls back to token detection /
    the explicit default. `store`/`source_or_none` are accepted for interface
    parity with the other tasks; this function does no store queries."""
    hints: dict[str, dict] = {}
    unsure: list[str] = []
    junk: list[dict] = []
    tail: list[str] = []
    pattern_hits = 0
    for path in relpaths:
        m = match_name_pattern(cfg, path.replace("\\", "/").split("/")[-1])
        if m is None:
            tail.append(path)
            continue
        kind, rule = m
        pattern_hits += 1
        if kind == JUNK:
            junk.append({"relpath": path, "why": rule})
        else:
            hints[path] = {"media_kind": kind, "language": None, "year": None}

    languages = tuple(dict.fromkeys(
        list(cfg.layout.languages.keys()) + [cfg.layout.default_language]))
    spec = _media_spec(languages)
    for start in range(0, len(tail), batch_size):
        chunk = tail[start:start + batch_size]
        user = "Identify these paths:\n" + "\n".join(
            f"{i}: {p}" for i, p in enumerate(chunk))
        out = run_task(client, spec, user)
        if out.value is None:
            unsure.extend(chunk)
            continue
        got = {it["i"]: it for it in out.value["items"]
               if 0 <= it["i"] < len(chunk)}
        for i, path in enumerate(chunk):
            it = got.get(i)
            if (it is None or it["media_kind"] == UNSURE
                    or it["confidence"] < CONFIDENCE_FLOOR):
                unsure.append(path)
                continue
            if it["media_kind"] == JUNK:
                junk.append({"relpath": path, "why": "model:junk"})
                continue
            hints[path] = {
                "media_kind": it["media_kind"],
                "language": None if it["language"] == UNSURE else it["language"],
                "year": it["year"],
            }
    return {"hints": hints, "unsure": unsure, "junk": junk,
            "pattern_hits": pattern_hits, "total": len(relpaths)}


# ── triage: dispositions for a REVIEW pile ──────────────────────────────────

DISPOSITIONS = ("keep-organize", "stage-junk", "needs-human")


@dataclass
class Cluster:
    cid: int
    top: str
    ext: str
    count: int
    bytes: int
    exemplars: list[str]


def digest_review(store: Store, cfg: Config, source_name: str,
                  max_clusters: int = 40) -> list[Cluster]:
    """Deterministic rollup: (top folder, extension) clusters, byte-weighted."""
    agg: dict[tuple[str, str], list] = defaultdict(lambda: [0, 0, []])
    for r in store.source_iter(source_name, "REVIEW"):
        rel = r["relpath"]
        parts = rel.replace("/", os.sep).split(os.sep)
        top = parts[0] if len(parts) > 1 else "(root)"
        ext = os.path.splitext(rel)[1].lower() or "(none)"
        cell = agg[(top, ext)]
        cell[0] += 1
        cell[1] += r["size"]
        if len(cell[2]) < 4:
            cell[2].append(rel)
    ranked = sorted(agg.items(), key=lambda kv: -kv[1][1])[:max_clusters]
    return [Cluster(i, top, ext, c, b, ex)
            for i, ((top, ext), (c, b, ex)) in enumerate(ranked)]


def _triage_spec() -> TaskSpec:
    def validate(obj: dict) -> dict:
        require_keys(obj, ("clusters",))
        if not isinstance(obj["clusters"], list):
            raise SchemaError("clusters must be a list")
        for c in obj["clusters"]:
            if not isinstance(c, dict):
                raise SchemaError("each cluster must be an object")
            require_keys(c, ("id", "disposition", "rationale", "confidence"))
            require_choice(c["disposition"], DISPOSITIONS, "disposition")
            require_confidence(c)
        return obj

    return TaskSpec(
        name="triage",
        system=(
            "You triage clusters of unclassified files from an old drive being "
            "consolidated into a personal library. For each cluster choose: "
            "'keep-organize' (personal/irreplaceable content worth keeping), "
            "'stage-junk' (caches, installers, recovery debris, scrape output "
            "— safe to stage for disposal), or 'needs-human' (genuinely "
            "ambiguous, or personal-looking data you are not sure is junk). "
            "Losing personal media is catastrophic; staging junk wrongly is "
            "merely untidy — when torn, choose needs-human. Reply ONLY with "
            'JSON: {"clusters": [{"id": <id>, "disposition": "<choice>", '
            '"rationale": "<short>", "confidence": <0..1>}, ...]}.'),
        validate=validate)


def _media_extensions(cfg: Config) -> set[str]:
    out: set[str] = set()
    for label in ("Photos", "Video", "Videos", "Audio"):
        out.update(cfg.taxonomy.get(label, ()))
    return out


def triage_review(client: ChainClient, store: Store, cfg: Config,
                  source_name: str) -> dict:
    clusters = digest_review(store, cfg, source_name)
    if not clusters:
        return {"decisions": [], "guarded": 0, "clusters": 0}
    lines = [f"{c.cid}: folder={c.top!r} ext={c.ext} files={c.count} "
             f"bytes={c.bytes:,} e.g. {'; '.join(c.exemplars[:3])}"
             for c in clusters]
    out = run_task(client, _triage_spec(),
                   "Clusters:\n" + "\n".join(lines))
    by_id = {c.cid: c for c in clusters}
    decisions, guarded = [], 0
    media = _media_extensions(cfg)
    if out.value is not None:
        for d in out.value["clusters"]:
            c = by_id.get(d["id"])
            if c is None:
                continue
            disposition = d["disposition"]
            # Deterministic dangerous-error guard (agent-design §5): junking
            # personal-media extensions demands near-certainty.
            if (disposition == "stage-junk" and c.ext in media
                    and d["confidence"] < DANGEROUS_GUARD):
                disposition = "needs-human"
                guarded += 1
            decisions.append({
                "id": c.cid, "top": c.top, "ext": c.ext, "count": c.count,
                "bytes": c.bytes, "disposition": disposition,
                "model_disposition": d["disposition"],
                "rationale": d["rationale"], "confidence": d["confidence"]})
    undecided = [c.cid for c in clusters
                 if c.cid not in {d["id"] for d in decisions}]
    for cid in undecided:
        c = by_id[cid]
        decisions.append({"id": cid, "top": c.top, "ext": c.ext,
                          "count": c.count, "bytes": c.bytes,
                          "disposition": "needs-human",
                          "model_disposition": None,
                          "rationale": "model did not answer", "confidence": 0.0})
    return {"decisions": sorted(decisions, key=lambda d: -d["bytes"]),
            "guarded": guarded, "clusters": len(clusters)}


# ── orchestrate: pick the next engine command ───────────────────────────────

def _orchestrate_spec(n_options: int) -> TaskSpec:
    def validate(obj: dict) -> dict:
        require_keys(obj, ("choice", "why"))
        c = obj["choice"]
        # bool is an int subclass — reject it so `true` can't select option 1.
        ok = c == "stop" or (isinstance(c, int) and not isinstance(c, bool)
                             and 0 <= c < n_options)
        if not ok:
            raise SchemaError(
                f"choice must be 'stop' or an integer 0..{n_options - 1}")
        return obj

    return TaskSpec(
        name="orchestrate",
        system=(
            "You operate a safety-first file-consolidation engine via its "
            "machine summary. Pick exactly one of the numbered suggested "
            "commands, or 'stop' if none is worth running. Reply ONLY with "
            'JSON: {"choice": <index or "stop">, "why": "<short>"}.'),
        validate=validate)


def next_action(client: ChainClient, summary: dict) -> dict:
    options = summary.get("suggested_next", [])
    if not options:
        return {"choice": "stop", "why": "engine suggests nothing"}
    user = ("Engine summary:\n" + json.dumps(
        {k: summary.get(k) for k in
         ("command", "counts", "drift", "residuals", "warnings", "exit_code")},
        indent=1, default=str)[:4000]
        + "\n\nSuggested commands:\n" + "\n".join(
            f"{i}: {o['cmd']}   ({o.get('why','')})"
            for i, o in enumerate(options)))
    out = run_task(client, _orchestrate_spec(len(options)), user)
    if out.value is None:
        return {"choice": "stop", "why": "model gave no valid answer"}
    return out.value
