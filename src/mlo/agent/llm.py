"""Provider adapters + the deterministic fallback chain (docs/agent-design.md §1).

Chain semantics: config-declared, tried in EXACT order, one attempt per entry
(plus one bounded, delayed retry on a transient HTTP 429/503 — P21/C60), no
auto-discovery. `local` is a positionable slot, skipped unless enabled.
Transport is stdlib urllib (JSON POST, no streaming) — zero runtime deps.
Keys come from environment variables only: MLO_ANTHROPIC_KEY, MLO_GEMINI_KEY,
MLO_OPENAI_KEY (with MLO_OPENAI_BASE_URL for openai-compatible endpoints).

Every call yields a ledger row (model, hops, latency, tokens) the caller
persists via report.py — chain behavior stays auditable after the fact.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from ..config import Config


class LLMDisabled(Exception):
    """[llm] enabled = false — agent features are opt-in. CLI maps to exit 2."""


class ChainExhausted(Exception):
    """Every chain entry failed or was skipped for this call."""


@dataclass
class LLMReply:
    text: str
    model: str
    entry: str                      # chain entry name that answered
    hops: int                       # entries tried before this one answered
    latency_s: float
    ledger: list[dict] = field(default_factory=list)


_KNOWN_CLOUD = {
    # entry-name prefix -> adapter
    "claude": "anthropic",
    "gemini": "gemini",
    "gpt": "openai-compatible",   # hosted OpenAI-compatible name
}


def _adapter_for(entry: str) -> str:
    if entry == "local":
        return "openai-compatible"
    for prefix, adapter in _KNOWN_CLOUD.items():
        if entry.startswith(prefix):
            return adapter
    return "openai-compatible"


def _post_json(url: str, payload: dict, headers: dict, timeout_s: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


_RETRY_MAX_DELAY_S = 10.0
_RETRY_SLEEP = time.sleep          # injectable for tests


def _retry_delay(e: "urllib.error.HTTPError") -> float:
    """Seconds to wait before the one bounded 429/503 retry: the server's
    Retry-After when present and sane, else 1s; always capped."""
    try:
        ra = float((e.headers or {}).get("Retry-After", ""))
    except (TypeError, ValueError):
        ra = 1.0
    return max(0.0, min(ra if ra > 0 else 1.0, _RETRY_MAX_DELAY_S))


@dataclass
class PreflightResult:
    entry: str
    ok: bool
    detail: str


def preflight(cfg: Config, transport=None) -> list[PreflightResult]:
    """Per-chain-entry reachability check (P21/B8): one cheap, low-token
    completion probe per entry — is a local Ollama endpoint reachable with
    the configured model pulled? Does a cloud entry's API key resolve to a
    working call? Never raises; each entry's outcome is reported
    independently so one broken entry doesn't stop the rest from being
    checked. Caller's responsibility: only call this when cfg.llm.enabled
    (ChainClient's own construction refuses otherwise, matching every other
    agent-layer entry point's kill-switch posture)."""
    client = ChainClient(cfg, transport=transport)
    out: list[PreflightResult] = []
    for entry in cfg.llm.chain:
        if entry == "local" and not cfg.llm.local.enabled:
            out.append(PreflightResult(entry, False, "skipped (disabled)"))
            continue
        try:
            _text, model = client._call_entry(
                entry, "You are a reachability probe.",
                "Reply with the single word: OK", 8)
            out.append(PreflightResult(entry, True, f"reachable (model {model})"))
        except urllib.error.HTTPError as e:
            out.append(PreflightResult(entry, False, f"HTTP {e.code}"))
        except (urllib.error.URLError, OSError, TimeoutError, KeyError,
                json.JSONDecodeError, ValueError) as e:
            out.append(PreflightResult(entry, False, f"{type(e).__name__}: {e}"))
    return out


def chain_config(cfg: Config, chain: tuple[str, ...] | None) -> Config:
    """cfg with the chain overridden — for a run that needs a SPECIFIC chain
    (e.g. critics on a frontier model: `--chain claude-opus-4-8,local`).

    Unlike evals.eval_config (which force-enables agents to MEASURE a
    configuration), this never touches the [llm] enabled kill-switch: a
    disabled config stays disabled and ChainClient still refuses. The local
    slot is skipped-if-disabled by ChainClient itself, so an override can
    narrow or reorder the chain but cannot wake a transport the owner turned
    off. No chain -> cfg unchanged."""
    if not chain:
        return cfg
    import dataclasses
    return dataclasses.replace(cfg, llm=dataclasses.replace(
        cfg.llm, chain=tuple(chain)))


class ChainClient:
    """One attempt per entry (a transient 429/503 gets one delayed retry,
    P21/C60); failures advance; UNSKIPPABLE honesty in the ledger."""

    def __init__(self, cfg: Config, transport=None):
        if not cfg.llm.enabled:
            raise LLMDisabled(
                "[llm] enabled = false — set it to true (and configure a chain) "
                "to use agent features; everything else works without them")
        self.cfg = cfg
        self._transport = transport or _post_json   # injectable for tests

    def has_local(self) -> bool:
        """True when the chain contains an ENABLED local entry. The 'strong'
        tier differs from the base tier only by skipping local entries, and
        self-consistency voting is only free on local tokens — both callers
        (protocol.run_task escalation, tasks.classify_unmatched voting) gate
        on this to avoid re-buying identical cloud calls."""
        return self.cfg.llm.local.enabled \
            and any(e == "local" for e in self.cfg.llm.chain)

    def complete(self, system: str, user: str, *, tier: str = "any",
                 max_tokens: int = 2048) -> LLMReply:
        ledger: list[dict] = []
        hops = 0
        for entry in self.cfg.llm.chain:
            if entry == "local" and not self.cfg.llm.local.enabled:
                ledger.append({"entry": entry, "outcome": "skipped-disabled"})
                continue
            if tier == "strong" and entry == "local":
                ledger.append({"entry": entry, "outcome": "skipped-tier"})
                continue
            reply = self._try_entry(entry, system, user, max_tokens, ledger, hops)
            if reply is not None:
                return reply
            hops += 1
        raise ChainExhausted(
            "no chain entry answered: "
            + "; ".join(f"{r['entry']}={r['outcome']}" for r in ledger))

    def _try_entry(self, entry: str, system: str, user: str, max_tokens: int,
                   ledger: list[dict], hops: int) -> LLMReply | None:
        """One chain entry, with ONE bounded retry (P21/B8) on a transient
        429/503 before giving up on this entry and letting the caller advance
        to the next. Returns an LLMReply on success, None once both attempts
        (or the single attempt, for a non-retryable failure) are exhausted."""
        for attempt in range(2):
            t0 = time.monotonic()
            try:
                text, model = self._call_entry(entry, system, user, max_tokens)
                dt = time.monotonic() - t0
                ledger.append({"entry": entry, "outcome": "ok",
                               "latency_s": round(dt, 3)})
                return LLMReply(text, model, entry, hops, dt, ledger)
            except urllib.error.HTTPError as e:
                dt = time.monotonic() - t0
                if attempt == 0 and e.code in (429, 503):
                    # An immediate retry against a window-exhausted rate
                    # limit almost always repeats the 429 — honor the
                    # server's Retry-After (capped) or wait a beat, so the
                    # retry has a chance and the entry isn't burned for
                    # nothing (super-review M15).
                    delay = _retry_delay(e)
                    ledger.append({"entry": entry, "outcome": "retrying",
                                   "error": f"HTTP {e.code}",
                                   "retry_delay_s": delay,
                                   "latency_s": round(dt, 3)})
                    _RETRY_SLEEP(delay)
                    continue
                ledger.append({"entry": entry, "outcome": "failed",
                               "error": f"HTTPError: {e}",
                               "latency_s": round(dt, 3)})
                return None
            except (urllib.error.URLError, OSError, TimeoutError, KeyError,
                    json.JSONDecodeError, ValueError) as e:
                ledger.append({"entry": entry, "outcome": "failed",
                               "error": f"{type(e).__name__}: {e}",
                               "latency_s": round(time.monotonic() - t0, 3)})
                return None
        return None            # pragma: no cover (loop always returns/continues)

    # ── adapters ─────────────────────────────────────────────────────────

    def _call_entry(self, entry: str, system: str, user: str,
                    max_tokens: int) -> tuple[str, str]:
        adapter = _adapter_for(entry)
        if adapter == "openai-compatible":
            return self._openai_compatible(entry, system, user, max_tokens)
        if adapter == "anthropic":
            return self._anthropic(entry, system, user, max_tokens)
        return self._gemini(entry, system, user, max_tokens)

    def _openai_compatible(self, entry, system, user, max_tokens):
        local = self.cfg.llm.local
        if entry == "local":
            url = local.url.rstrip("/") + "/v1/chat/completions"
            model = local.model
            timeout = local.timeout_s
            extra = {"keep_alive": local.keep_alive,
                     "reasoning_effort": local.reasoning_effort}
            options = {"num_ctx": local.num_ctx}
        else:
            url = os.environ.get("MLO_OPENAI_BASE_URL",
                                 "https://api.openai.com").rstrip("/") \
                + "/v1/chat/completions"
            model, timeout, extra, options = entry, 120, {}, None
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
            **extra,
        }
        if options:
            payload["options"] = options            # Ollama extension
        headers = {}
        key = os.environ.get("MLO_OPENAI_KEY")
        if key and entry != "local":
            headers["Authorization"] = f"Bearer {key}"
        data = self._transport(url, payload, headers, timeout)
        return data["choices"][0]["message"]["content"], model

    def _anthropic(self, entry, system, user, max_tokens):
        key = os.environ.get("MLO_ANTHROPIC_KEY")
        if not key:
            raise ValueError("MLO_ANTHROPIC_KEY not set")
        data = self._transport(
            "https://api.anthropic.com/v1/messages",
            {"model": entry, "system": system, "max_tokens": max_tokens,
             "temperature": 0.2,      # pinned like the other adapters —
                                      # provider default 1.0 raises repair
                                      # retries on the priciest entries
             "messages": [{"role": "user", "content": user}]},
            {"x-api-key": key, "anthropic-version": "2023-06-01"}, 120)
        return "".join(b.get("text", "") for b in data["content"]), entry

    def _gemini(self, entry, system, user, max_tokens):
        key = os.environ.get("MLO_GEMINI_KEY")
        if not key:
            raise ValueError("MLO_GEMINI_KEY not set")
        # Key travels in a header, never the URL query string (a query key leaks
        # into proxy/URL logs and any exception text the ledger formats).
        data = self._transport(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{entry}:generateContent",
            {"system_instruction": {"parts": [{"text": system}]},
             "contents": [{"parts": [{"text": user}]}],
             "generationConfig": {"maxOutputTokens": max_tokens,
                                  "temperature": 0.2}},
            {"x-goog-api-key": key}, 120)
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts), entry
