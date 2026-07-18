"""The reliability protocol against a scripted endpoint: repair, escalation,
abstention, self-consistency, kill-switch, chain semantics."""
from __future__ import annotations

import dataclasses
import json

import pytest

from helpers_plan import make_cfg
from mlo.agent.llm import ChainClient, ChainExhausted, LLMDisabled
from mlo.agent.protocol import (SchemaError, TaskSpec, extract_json,
                                majority_vote, require_keys, run_task)


def llm_cfg(world, chain=("local",), local_enabled=True):
    cfg = make_cfg(world)
    return dataclasses.replace(
        cfg, llm=dataclasses.replace(
            cfg.llm, enabled=True, chain=tuple(chain),
            local=dataclasses.replace(cfg.llm.local, enabled=local_enabled)))


def scripted(replies):
    """Transport that plays back canned reply texts, then repeats the last."""
    it = list(replies)
    calls = []

    def transport(url, payload, headers, timeout_s):
        calls.append(payload)
        text = it.pop(0) if len(it) > 1 else it[0]
        return {"choices": [{"message": {"content": text}}]}

    transport.calls = calls
    return transport


SPEC = TaskSpec(name="t", system="sys",
                validate=lambda d: (require_keys(d, ("answer",)), d)[1])


def test_kill_switch(world):
    cfg = make_cfg(world)                       # llm disabled by default
    with pytest.raises(LLMDisabled):
        ChainClient(cfg)


def test_local_slot_skipped_when_disabled(world):
    cfg = llm_cfg(world, chain=("local",), local_enabled=False)
    client = ChainClient(cfg, transport=scripted(['{"answer": 1}']))
    with pytest.raises(ChainExhausted, match="skipped-disabled"):
        client.complete("s", "u")


def test_strong_tier_skips_local(world):
    cfg = llm_cfg(world)
    client = ChainClient(cfg, transport=scripted(['{"answer": 1}']))
    with pytest.raises(ChainExhausted, match="skipped-tier"):
        client.complete("s", "u", tier="strong")


def test_chain_advances_on_failure(world):
    cfg = llm_cfg(world, chain=("gemini-x", "local"))   # no key -> gemini fails
    client = ChainClient(cfg, transport=scripted(['{"answer": 42}']))
    reply = client.complete("s", "u")
    assert reply.entry == "local" and reply.hops == 1
    assert reply.ledger[0]["outcome"] == "failed"


def test_repair_loop_fixes_bad_json(world):
    cfg = llm_cfg(world)
    client = ChainClient(cfg, transport=scripted(
        ["not json at all", '{"answer": "fixed"}']))
    out = run_task(client, SPEC, "question")
    assert out.value == {"answer": "fixed"}
    assert out.attempts == 2 and not out.unsure
    # the repair prompt carried the validator error back to the model
    assert "failed validation" in client._transport.calls[1]["messages"][1]["content"]


def test_never_valid_becomes_unsure(world):
    cfg = llm_cfg(world)
    client = ChainClient(cfg, transport=scripted(["{}"]))   # persistently wrong
    out = run_task(client, SPEC, "question")
    assert out.unsure and out.value is None
    assert out.escalated


# ── retry on transient 429/503 (P21/B8) ──────────────────────────────────────

def test_transient_429_retries_once_then_succeeds(world, monkeypatch):
    import urllib.error

    from mlo.agent import llm as llmmod

    calls = []
    slept = []
    monkeypatch.setattr(llmmod, "_RETRY_SLEEP", slept.append)

    def flaky(url, payload, headers, timeout_s):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(url, 429, "rate limited",
                                         {"Retry-After": "3"}, None)
        return {"choices": [{"message": {"content": '{"answer": 1}'}}]}

    cfg = llm_cfg(world)
    client = ChainClient(cfg, transport=flaky)
    reply = client.complete("s", "u")
    assert reply.text == '{"answer": 1}'
    assert len(calls) == 2                          # one retry, then success
    # the retry WAITED, honoring Retry-After (super-review M15) — an
    # immediate retry against a window-exhausted limit repeats the 429
    assert slept == [3.0]
    assert reply.ledger[0]["outcome"] == "retrying"
    assert reply.ledger[1]["outcome"] == "ok"


def test_persistent_429_gives_up_after_one_retry_and_advances(world, monkeypatch):
    import urllib.error

    from mlo.agent import llm as llmmod
    monkeypatch.setattr(llmmod, "_RETRY_SLEEP", lambda s: None)

    def always_429(url, payload, headers, timeout_s):
        raise urllib.error.HTTPError(url, 429, "rate limited", {}, None)

    cfg = llm_cfg(world, chain=("local", "gemini-x"))    # no gemini key -> also fails
    client = ChainClient(cfg, transport=always_429)
    with pytest.raises(ChainExhausted):
        client.complete("s", "u")


def test_non_retryable_http_error_fails_immediately_no_retry(world):
    import urllib.error

    calls = []

    def bad_request(url, payload, headers, timeout_s):
        calls.append(1)
        raise urllib.error.HTTPError(url, 400, "bad request", {}, None)

    cfg = llm_cfg(world, chain=("local",))
    client = ChainClient(cfg, transport=bad_request)
    with pytest.raises(ChainExhausted):
        client.complete("s", "u")
    assert len(calls) == 1                           # no retry for a non-429/503


# ── preflight (P21/B8) ────────────────────────────────────────────────────

def test_preflight_reports_reachable_entry(world):
    from mlo.agent.llm import preflight
    cfg = llm_cfg(world)
    results = preflight(cfg, transport=scripted(['{"answer": 1}']))
    assert len(results) == 1
    assert results[0].entry == "local" and results[0].ok


def test_preflight_reports_unreachable_entry_without_raising(world):
    from mlo.agent.llm import preflight

    def boom(url, payload, headers, timeout_s):
        raise OSError("connection refused")
    cfg = llm_cfg(world)
    results = preflight(cfg, transport=boom)
    assert len(results) == 1
    assert results[0].ok is False and "OSError" in results[0].detail


def test_preflight_reports_disabled_local_without_probing(world):
    from mlo.agent.llm import preflight
    cfg = llm_cfg(world, chain=("local",), local_enabled=False)
    calls = []

    def tracker(url, payload, headers, timeout_s):
        calls.append(1)
        return {"choices": [{"message": {"content": "{}"}}]}
    results = preflight(cfg, transport=tracker)
    assert results[0].ok is False and "disabled" in results[0].detail
    assert not calls                                 # never actually probed


def test_preflight_checks_every_entry_independently(world, monkeypatch):
    from mlo.agent.llm import preflight

    monkeypatch.delenv("MLO_GEMINI_KEY", raising=False)
    calls = {"n": 0}

    def transport(url, payload, headers, timeout_s):
        calls["n"] += 1
        raise OSError("local entry down")

    cfg = llm_cfg(world, chain=("local", "gemini-x"))
    results = preflight(cfg, transport=transport)
    assert len(results) == 2
    # both entries were probed independently (not short-circuited after the
    # first failure): 'local' fails via the transport, 'gemini-x' fails
    # earlier (no key configured) without ever reaching the transport.
    assert results[0].entry == "local" and results[0].ok is False
    assert results[1].entry == "gemini-x" and results[1].ok is False
    assert "not set" in results[1].detail
    assert calls["n"] == 1


def test_majority_vote_wins_and_ties_abstain(world):
    cfg = llm_cfg(world)
    win = ChainClient(cfg, transport=scripted(
        ['{"answer": "A"}', '{"answer": "B"}', '{"answer": "A"}']))
    out = majority_vote(win, SPEC, "q", key=lambda v: v["answer"])
    assert out.value == {"answer": "A"} and not out.unsure

    tie = ChainClient(cfg, transport=scripted(
        ['{"answer": "A"}', '{"answer": "B"}', '{"answer": "C"}']))
    out = majority_vote(tie, SPEC, "q", key=lambda v: v["answer"])
    assert out.unsure and out.value is None     # honest tie, no coin flip


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('sure!\n```json\n{"a": 2}\n```\nthanks') == {"a": 2}
    assert extract_json('preamble {"a": {"b": 3}} trailer') == {"a": {"b": 3}}
    with pytest.raises(SchemaError):
        extract_json("no object here")
    with pytest.raises(SchemaError):
        extract_json('["top-level array"]')


# -- chain_config: the critics/pilot chain override (Task #12) ----------------

def test_chain_config_overrides_chain_only():
    """chain_config swaps the chain and NOTHING else — enabled stays as
    configured (the kill-switch is never overridden), local settings intact."""
    from mlo.agent.llm import chain_config
    # a minimal cfg built directly (no world needed for pure config surgery)
    from mlo.config import Config, Layout, LLM
    cfg = Config(
        library_root="X", sources=(), staging={}, protected_substrings=(),
        protected_drives=(), junk_zero_byte=True, junk_names=(),
        junk_extensions=(), max_unmatched_pct=5.0, taxonomy={},
        layout=Layout(), llm=LLM(enabled=False, chain=("local",)),
        config_hash="h", path="p")
    out = chain_config(cfg, ("claude-opus-4-8", "local"))
    assert out.llm.chain == ("claude-opus-4-8", "local")
    assert out.llm.enabled is False           # kill-switch untouched
    assert out.llm.local.enabled is False     # local slot config untouched
    assert chain_config(cfg, None) is cfg     # no override -> same cfg


def test_chain_config_disabled_cfg_still_refuses(world):
    """An override cannot wake a disabled agent layer: ChainClient refuses."""
    import pytest
    from mlo.agent.llm import ChainClient, LLMDisabled, chain_config
    cfg = llm_cfg(world)
    import dataclasses
    disabled = dataclasses.replace(
        cfg, llm=dataclasses.replace(cfg.llm, enabled=False))
    with pytest.raises(LLMDisabled):
        ChainClient(chain_config(disabled, ("claude-opus-4-8",)))


def test_chain_config_cannot_wake_disabled_local(world):
    """'local' in the override while [llm.local] enabled=false: the slot is
    skipped-disabled by ChainClient, not woken."""
    from mlo.agent.llm import ChainClient, ChainExhausted, chain_config
    import pytest
    cfg = llm_cfg(world, chain=("local",), local_enabled=False)
    client = ChainClient(chain_config(cfg, ("local",)),
                         transport=scripted(["never-reached"]))
    with pytest.raises(ChainExhausted):
        client.complete("s", "u")
