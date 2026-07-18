"""identify — the productized identification loop (P21/B6): slice a
review-set into batches, run the critic chain, merge hints. Same scripted-
client pattern as test_pilot.py's critic tests."""
from __future__ import annotations

import json
import os

from mlo import identify as identifymod
from mlo.config import Config, Layout, LLM, LocalLLM


def _cfg(tmp_path) -> Config:
    return Config(
        library_root=str(tmp_path / "lib"), sources=(), staging={},
        protected_substrings=(), protected_drives=(), junk_zero_byte=True,
        junk_names=(), junk_extensions=(), max_unmatched_pct=5.0,
        taxonomy={"Video": (".mkv",)}, layout=Layout(),
        llm=LLM(enabled=True, chain=("local",),
               local=LocalLLM(enabled=True)),
        config_hash="h", path=str(tmp_path / "mlo.toml"))


def _write_review_set(path, items):
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"schema": "mlo.review-set/1"}) + "\n")
        for it in items:
            f.write(json.dumps(it) + "\n")


def _video_item(rel: str) -> dict:
    return {"relpath": rel, "ext": ".mkv", "size": 100, "quick_hash": "q",
           "bucket": "Video", "language_guess": "English", "origin": None,
           "origin_signal": None, "candidate_homes": ["Video/Movies/English"]}


class _Scripted:
    """A ChainClient stand-in that always returns one canned reply."""
    def __init__(self, cfg, transport=None):
        pass

    def has_local(self):
        return True

    def complete(self, system, user, *, tier="any", max_tokens=2048):
        from mlo.agent.llm import LLMReply
        reply = {"media_kind": "movie", "language": "English", "year": 2010,
                 "title": "Inception", "proposed_home": "Video/Movies/English",
                 "confidence": 0.95, "rationale": "clear"}
        return LLMReply(json.dumps(reply), "scripted", "local", 0, 0.01)


# ── read_review_set ──────────────────────────────────────────────────────────

def test_read_review_set_skips_header_line(tmp_path):
    p = tmp_path / "rs.jsonl"
    _write_review_set(p, [_video_item("a.mkv"), _video_item("b.mkv")])
    items = identifymod.read_review_set(str(p))
    assert len(items) == 2
    assert {it["relpath"] for it in items} == {"a.mkv", "b.mkv"}


def test_read_review_set_skips_blank_lines(tmp_path):
    p = tmp_path / "rs.jsonl"
    p.write_text('{"schema": "x"}\n\n' + json.dumps(_video_item("a.mkv")) + "\n\n",
                encoding="utf-8")
    items = identifymod.read_review_set(str(p))
    assert len(items) == 1


def test_read_review_set_missing_file_raises_config_error(tmp_path):
    import pytest
    from mlo.config import ConfigError
    with pytest.raises(ConfigError, match="cannot read review-set"):
        identifymod.read_review_set(str(tmp_path / "nope.jsonl"))


# ── identify(): batching + merge ────────────────────────────────────────────

def test_identify_batches_and_merges_hints(tmp_path, monkeypatch):
    monkeypatch.setattr(identifymod, "ChainClient", _Scripted)
    cfg = _cfg(tmp_path)
    items = [_video_item(f"m{i}.mkv") for i in range(5)]
    rs = tmp_path / "rs.jsonl"
    _write_review_set(rs, items)

    merged, res = identifymod.identify(cfg, str(rs), batch_size=2)

    assert res.items == 5
    assert res.batches == 3           # ceil(5/2)
    assert res.hinted == 5
    assert set(merged) == {it["relpath"] for it in items}
    assert merged["m0.mkv"]["media_kind"] == "movie"


def test_identify_progress_callback_fires_per_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(identifymod, "ChainClient", _Scripted)
    cfg = _cfg(tmp_path)
    rs = tmp_path / "rs.jsonl"
    _write_review_set(rs, [_video_item(f"m{i}.mkv") for i in range(4)])

    calls = []
    identifymod.identify(cfg, str(rs), batch_size=2,
                         progress=lambda phase, info: calls.append((phase, info)))
    assert len(calls) == 2
    assert calls[0][0] == "identify-batch"
    assert calls[0][1]["n"] == 1 and calls[1][1]["n"] == 2


def test_identify_seeds_from_prior_hints(tmp_path, monkeypatch):
    monkeypatch.setattr(identifymod, "ChainClient", _Scripted)
    cfg = _cfg(tmp_path)
    prior = tmp_path / "prior.json"
    prior.write_text(json.dumps({
        "old/already-known.mkv": {"media_kind": "personal", "language": "English"}
    }), encoding="utf-8")
    rs = tmp_path / "rs.jsonl"
    _write_review_set(rs, [_video_item("new.mkv")])

    merged, res = identifymod.identify(cfg, str(rs), prior_hints_path=str(prior))

    # load_hints normalizes '/' to the host separator when parsing the prior
    # hints file — the same convention every other hints consumer follows.
    key = "old/already-known.mkv".replace("/", os.sep)
    assert key in merged
    assert merged[key]["media_kind"] == "personal"
    assert "new.mkv" in merged
    assert res.hinted == 2


def test_identify_empty_review_set_yields_empty_result(tmp_path, monkeypatch):
    monkeypatch.setattr(identifymod, "ChainClient", _Scripted)
    cfg = _cfg(tmp_path)
    rs = tmp_path / "rs.jsonl"
    _write_review_set(rs, [])
    merged, res = identifymod.identify(cfg, str(rs))
    assert merged == {} and res.items == 0 and res.batches == 0
