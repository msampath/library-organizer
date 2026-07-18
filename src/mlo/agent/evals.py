"""The eval harness (docs/agent-design.md §5): quality is measured, not claimed.

Golden sets live in evals/*.json. Metrics are asymmetric on purpose: a
DANGEROUS ERROR (personal content dispositioned as junk) is scored separately
from ordinary inaccuracy, because the two failure modes have wildly different
costs. A chain that abstains on baby videos beats one that guesses.

`heuristic_transport` is the deterministic mock endpoint: it lets CI regress
the harness math and the whole protocol stack with zero live models.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re

from ..config import Config
from .critics import run_panel
from .llm import ChainClient
from .protocol import UNSURE, run_task
from .tasks import _classify_spec, _triage_spec


def eval_config(cfg: Config, chain: tuple[str, ...] | None = None) -> Config:
    """A config forced agent-on for an evaluation run.

    With no `chain`, the default: local slot active over the configured chain
    (or a bare `("local",)`) — the local-only measurement CI and the golden-set
    baseline use. Pass `chain` to measure a SPECIFIC configuration (e.g.
    `("local", "claude-haiku-4-5")` for the escalation row, or
    `("claude-haiku-4-5",)` for cloud-only); the local slot is enabled only when
    `"local"` is in the chain, so a cloud-only row never wakes Ollama."""
    if chain:
        local_on = "local" in chain
    else:
        chain, local_on = cfg.llm.chain or ("local",), True
    return dataclasses.replace(
        cfg, llm=dataclasses.replace(
            cfg.llm, enabled=True, chain=tuple(chain),
            local=dataclasses.replace(cfg.llm.local, enabled=local_on)))


# ── the deterministic mock endpoint ─────────────────────────────────────────

_JUNKISH = re.compile(
    r"cache|thumbnail|thumbs\.db|\.tmp|spotify|installer|setup|\.part|"
    r"recovered|\$usnjrnl|desktop\.ini", re.IGNORECASE)
_EXT_LABELS = {
    ".mp4": "Video", ".mkv": "Video", ".avi": "Video", ".vob": "Video",
    ".mp3": "Audio", ".flac": "Audio", ".amr": "Audio",
    ".jpg": "Photos", ".png": "Photos", ".heic": "Photos",
    ".pdf": "Documents", ".docx": "Documents", ".txt": "Documents",
    ".zip": "Backups", ".crypt8": "Backups",
}


def heuristic_transport(url: str, payload: dict, headers: dict,
                        timeout_s: int) -> dict:
    """Rule-based stand-in for a model server (OpenAI-compatible shape)."""
    user = payload["messages"][-1]["content"]
    system = payload["messages"][0]["content"]
    sys_l = system.lower()
    if "triage clusters" in sys_l or '"clusters"' in system:
        answer = _heuristic_triage(user)
    elif "film and television critic" in sys_l:
        answer = _heuristic_critic_movie(user)
    elif "music librarian" in sys_l:
        answer = _heuristic_critic_music(user)
    elif "photo archivist" in sys_l:
        answer = _heuristic_critic_photo(user)
    else:
        answer = _heuristic_classify(user)
    return {"choices": [{"message": {"content": json.dumps(answer)}}]}


# P21/B7: heuristic critic-panel stand-ins (same posture as the classify/
# triage mocks above — deterministic and schema-shaped, not a claim of real
# accuracy; they let eval_critics/the harness math itself be regressed in CI
# with zero live models).
_PERSONAL_PATTERNS = re.compile(
    r"dashcam|whatsapp|\bwa0\d+\b|\bvid_2\d{3}|\bimg_2\d{3}", re.IGNORECASE)


def _heuristic_critic_movie(user: str) -> dict:
    view = json.loads(user)
    homes = view.get("candidate_homes") or [None]
    path = view.get("path") or ""
    kind = "personal" if _PERSONAL_PATTERNS.search(path) else "movie"
    # language/year/title must be UNSURE or a real value — the schema
    # rejects a bare None for language (require_choice has no None case).
    return {"media_kind": kind, "language": UNSURE, "year": None,
            "title": None, "proposed_home": homes[0], "confidence": 0.8,
            "rationale": "heuristic"}


def _heuristic_critic_music(user: str) -> dict:
    view = json.loads(user)
    homes = view.get("candidate_homes") or [None]
    path = view.get("path") or ""
    kind = "personal" if _PERSONAL_PATTERNS.search(path) else "music"
    return {"media_kind": kind, "language": UNSURE, "artist": None,
            "album": None, "proposed_home": homes[0], "confidence": 0.8,
            "rationale": "heuristic"}


_SCREENSHOT_PATTERNS = re.compile(r"screenshot|scrnshot", re.IGNORECASE)
_GRAPHIC_PATTERNS = re.compile(r"icon|logo|sticker|meme|wallpaper",
                               re.IGNORECASE)


def _heuristic_critic_photo(user: str) -> dict:
    view = json.loads(user)
    homes = view.get("candidate_homes") or [None]
    path = view.get("path") or ""
    if _SCREENSHOT_PATTERNS.search(path):
        kind = "screenshot"
    elif _GRAPHIC_PATTERNS.search(path):
        kind = "graphic"
    else:
        kind = "photo"
    return {"kind": kind, "year": None, "device": None,
            "proposed_home": homes[0], "confidence": 0.8,
            "rationale": "heuristic"}


def _heuristic_classify(user: str) -> dict:
    items = []
    for line in user.splitlines():
        m = re.match(r"^(\d+): (.+)$", line)
        if not m:
            continue
        i, path = int(m.group(1)), m.group(2)
        ext = os.path.splitext(path)[1].lower()
        label = _EXT_LABELS.get(ext, UNSURE)
        conf = 0.9 if label != UNSURE else 0.3
        items.append({"i": i, "label": label, "confidence": conf})
    return {"items": items}


def _heuristic_triage(user: str) -> dict:
    clusters = []
    for line in user.splitlines():
        m = re.match(r"^(\d+): folder=(.+?) ext=(\S+) files=(\d+) bytes=([\d,]+)",
                     line)
        if not m:
            continue
        cid, top, ext = int(m.group(1)), m.group(2), m.group(3)
        if _JUNKISH.search(line):
            d, conf = "stage-junk", 0.9
        elif ext in _EXT_LABELS:
            d, conf = "keep-organize", 0.85
        else:
            d, conf = "needs-human", 0.4
        clusters.append({"id": cid, "disposition": d,
                         "rationale": "heuristic", "confidence": conf})
    return {"clusters": clusters}


# ── runners ─────────────────────────────────────────────────────────────────

def eval_classify(client: ChainClient, golden_path: str,
                  labels: tuple[str, ...]) -> dict:
    golden = json.load(open(golden_path, encoding="utf-8"))
    spec = _classify_spec(labels)
    decided = correct = abstained = ambiguous_ok = 0
    ledger: list[dict] = []
    for start in range(0, len(golden), 20):
        chunk = golden[start:start + 20]
        user = "Label these paths:\n" + "\n".join(
            f"{i}: {g['path']}" for i, g in enumerate(chunk))
        out = run_task(client, spec, user)
        ledger.extend(out.ledger)
        answers = {}
        if out.value:
            answers = {it["i"]: it["label"] for it in out.value["items"]}
        for i, g in enumerate(chunk):
            got = answers.get(i, UNSURE)
            if g["gold"] == "AMBIGUOUS":
                if got == UNSURE:
                    ambiguous_ok += 1
                continue
            if got == UNSURE:
                abstained += 1
                continue
            decided += 1
            if got == g["gold"]:
                correct += 1
    labeled = [g for g in golden if g["gold"] != "AMBIGUOUS"]
    ambiguous = len(golden) - len(labeled)
    return {
        "task": "classify", "items": len(golden),
        "accuracy_on_decided": round(correct / decided, 3) if decided else None,
        "decided": decided, "abstained": abstained,
        "abstention_rate": round(abstained / len(labeled), 3) if labeled else 0,
        "ambiguous_handled": f"{ambiguous_ok}/{ambiguous}",
        "ledger": ledger,
    }


def eval_critics(client: ChainClient, cfg: Config, golden_path: str, *,
                 cross_check: bool = False) -> dict:
    """P21/B7: the critic-PANEL accuracy runner — there was none before this
    (G16). Runs run_panel over a review-set-shaped golden set (each item the
    same shape `seam.build_review_set` produces, plus a `gold` media_kind or
    'AMBIGUOUS'), scores the resolved media_kind against gold. Same
    asymmetric posture as eval_classify: an AMBIGUOUS gold item credits only
    a correct abstention, never a guess."""
    golden = json.load(open(golden_path, encoding="utf-8"))
    items = [{k: v for k, v in g.items() if k != "gold"} for g in golden]
    out = run_panel(client, cfg, items, cross_check=cross_check)
    # out['answers'] carries the full validated critic reply (media_kind OR,
    # for the photo critic, 'kind') — unlike out['hints'], which narrows a
    # genuine photo's media_kind to None (correct for the router, useless for
    # scoring "did it correctly call this a photo").
    answered = out["answers"]
    decided = correct = abstained = ambiguous_ok = 0
    for g in golden:
        rel = g["relpath"]
        gold = g["gold"]
        got = answered.get(rel)
        if gold == "AMBIGUOUS":
            if got is None:
                ambiguous_ok += 1
            continue
        if got is None:
            abstained += 1
            continue
        decided += 1
        if got.get("media_kind", got.get("kind")) == gold:
            correct += 1
    labeled = [g for g in golden if g["gold"] != "AMBIGUOUS"]
    ambiguous = len(golden) - len(labeled)
    return {
        "task": "critics", "items": len(golden),
        "accuracy_on_decided": round(correct / decided, 3) if decided else None,
        "decided": decided, "abstained": abstained,
        "abstention_rate": round(abstained / len(labeled), 3) if labeled else 0,
        "ambiguous_handled": f"{ambiguous_ok}/{ambiguous}",
        "dissent": len(out["dissent"]),
    }


def eval_triage(client: ChainClient, golden_path: str, media_exts: set[str],
                dangerous_guard: float = 0.95) -> dict:
    golden = json.load(open(golden_path, encoding="utf-8"))
    spec = _triage_spec()
    lines = [f"{i}: folder={g['top']!r} ext={g['ext']} files={g['count']} "
             f"bytes={g['bytes']:,} e.g. {'; '.join(g['exemplars'][:3])}"
             for i, g in enumerate(golden)]
    out = run_task(client, spec, "Clusters:\n" + "\n".join(lines))
    ledger = list(out.ledger)
    got: dict[int, dict] = {}
    if out.value:
        got = {c["id"]: c for c in out.value["clusters"]
               if isinstance(c.get("id"), int)}
    decided = correct = dangerous = guarded = needs_human = 0
    for i, g in enumerate(golden):
        d = got.get(i)
        pred = d["disposition"] if d else "needs-human"
        if (d and pred == "stage-junk" and g["ext"] in media_exts
                and d["confidence"] < dangerous_guard):
            pred = "needs-human"
            guarded += 1
        if pred == "needs-human":
            needs_human += 1
            continue
        decided += 1
        if pred == g["gold"]:
            correct += 1
        if pred == "stage-junk" and g["gold"] == "keep-organize":
            dangerous += 1
    return {
        "task": "triage", "clusters": len(golden),
        "accuracy_on_decided": round(correct / decided, 3) if decided else None,
        "decided": decided, "needs_human": needs_human, "guarded": guarded,
        "dangerous_errors": dangerous,
        "ledger": ledger,
    }
