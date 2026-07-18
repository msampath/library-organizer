"""The ledger is the contract, so the contract must be enforceable: every
`tests/<file>::<test>` named in docs/defect-ledger.md must actually collect.

This is the test the ledger itself demands ("A ledger entry without a passing
named test is an open wound — CI should say so"). It parses the ledger, extracts
every cited test reference, and asserts each one exists via pytest collection.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
LEDGER = ROOT / "docs" / "defect-ledger.md"
REF = re.compile(r"(test_[A-Za-z0-9_]+\.py)::(test_[A-Za-z0-9_]+)")


def _cited_refs() -> list[tuple[str, str]]:
    text = LEDGER.read_text(encoding="utf-8")
    return sorted(set(REF.findall(text)))


def _defs_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}


def test_ledger_cites_some_tests():
    assert _cited_refs(), "no test references found in the defect ledger"


def test_every_ledger_named_test_exists():
    missing = []
    for fname, tname in _cited_refs():
        f = TESTS / fname
        if not f.exists() or tname not in _defs_in(f):
            missing.append(f"{fname}::{tname}")
    assert not missing, (
        "defect-ledger.md cites tests that do not exist (rename the test or the "
        "citation so the contract is real):\n  " + "\n  ".join(missing))
