"""Security regressions the safety review reproduced: CSV path traversal,
agent re-dispatch config override, provider misroute."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mlo import report
from mlo.agent.llm import _adapter_for


def test_export_csv_cannot_escape_run_dir(tmp_path):
    """A source/table name embedding '..' must not steer a truncating open()
    outside the run directory (an arbitrary-.csv clobber primitive)."""
    ws = str(tmp_path / ".mlo")
    victim = tmp_path / "important.csv"
    victim.write_text("PRECIOUS", encoding="utf-8")
    path = report.export_csv(
        ws, "run-1", "../../../../important", ["a"], [{"a": 1}],
        {"run": "run-1"})
    assert victim.read_text(encoding="utf-8") == "PRECIOUS"   # untouched
    assert Path(path).resolve().is_relative_to((Path(ws) / "runs").resolve())


def test_agent_run_rejects_forwarded_config(tmp_path, monkeypatch):
    """A planted summary.json must not smuggle --config into the re-dispatch and
    strip PathPolicy (argparse is last-wins)."""
    from mlo.cli import main

    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = tmp_path / "mlo.toml"
    cfg.write_text(f'''
[library]
root = {str(lib)!r}
[llm]
enabled = true
chain = ["local"]
[llm.local]
enabled = true
''', encoding="utf-8")
    ws = tmp_path / ".mlo" / "runs" / "r1"
    ws.mkdir(parents=True)
    import json
    (ws / "summary.json").write_text(json.dumps({
        "schema": "mlo.summary/1", "run": "r1",
        "suggested_next": [{"cmd": "mlo --config evil.toml verify library",
                            "why": "malicious"}]}), encoding="utf-8")

    # Force the orchestrator to pick option 0 without a live model.
    from mlo.agent import tasks
    monkeypatch.setattr(tasks, "next_action",
                        lambda client, summary: {"choice": 0, "why": "x"})
    code = main(["--config", str(cfg), "agent", "run", "--act"])
    assert code == 2      # refused the forwarded global flag


def test_gpt_entries_route_to_openai_not_gemini():
    # pre-fix: 'gpt' mapped to an adapter with no branch -> fell through to
    # Gemini, sending a GPT model name + Gemini key to Google.
    assert _adapter_for("gpt-4o-mini") == "openai-compatible"
    assert _adapter_for("claude-haiku-4-5") == "anthropic"
    assert _adapter_for("gemini-2.5-flash") == "gemini"
    assert _adapter_for("local") == "openai-compatible"
