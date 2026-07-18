"""Plan artifact integrity (hypothesis): round-trip identity, tamper detection."""
from __future__ import annotations

from hypothesis import given, settings, strategies as st

from mlo.report import PlanIntegrityError, read_plan, write_plan

import pytest

pathish = st.text(
    alphabet=st.characters(blacklist_characters="\x00\r\n",
                           min_codepoint=1, max_codepoint=0x10FFFF,
                           blacklist_categories=()),
    min_size=1, max_size=60)

row_strategy = st.fixed_dictionaries({
    "op_id": st.text("abcdef0123456789", min_size=8, max_size=8),
    "kind": st.sampled_from(["stage_move", "copy_in", "move_within"]),
    "src": pathish,
    "dst": pathish,
    "pre": st.fixed_dictionaries({
        "size": st.integers(min_value=0, max_value=2**40),
        "quick_hash": st.text("0123456789abcdef", min_size=64, max_size=64),
    }),
    "reason": st.fixed_dictionaries({
        "verdict": st.sampled_from(["ORGANIZED", "JUNK", "UNIQUE"]),
        "rule": st.text(max_size=20),
    }),
})


@settings(max_examples=25, deadline=None)
@given(st.lists(row_strategy, max_size=8))
def test_write_read_identity(tmp_path_factory, rows):
    ws = str(tmp_path_factory.mktemp("ws"))
    path, plan_id = write_plan(ws, "dedup", "src", "cfg", [], rows)
    header, back, got_id = read_plan(path)
    assert back == rows
    assert got_id == plan_id
    assert header["schema"] == "mlo.plan/1"


def test_tampered_plan_refused(tmp_path):
    path, _ = write_plan(str(tmp_path), "dedup", "s", "cfg", [],
                         [{"op_id": "aa", "kind": "copy_in", "src": "a",
                           "dst": "b", "pre": {}, "reason": {}}])
    raw = open(path, "rb").read()
    open(path, "wb").write(raw.replace(b'"src": "a"', b'"src": "Z"'))
    with pytest.raises(PlanIntegrityError, match="hash mismatch"):
        read_plan(path)


def test_truncated_plan_refused(tmp_path):
    path, _ = write_plan(str(tmp_path), "dedup", "s", "cfg", [],
                         [{"op_id": "aa", "kind": "copy_in", "src": "a",
                           "dst": "b", "pre": {}, "reason": {}}] * 3)
    lines = open(path, "rb").read().splitlines(keepends=True)
    open(path, "wb").writelines(lines[:1] + lines[2:])   # drop a body row
    with pytest.raises(PlanIntegrityError):
        read_plan(path)
