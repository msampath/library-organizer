"""Shared fixtures: fake drives inside one tmp dir, a real store, a kernel factory.

Same-drive rules are testable anywhere because PathPolicy takes an injectable
drive_of: any path under <tmp>/driveX/... reports drive 'X'.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mlo.safeops import PathPolicy, SafeOps  # noqa: E402
from mlo.store import Store  # noqa: E402


@pytest.fixture()
def world(tmp_path):
    """A little universe: two 'drives', a library, staging roots, a store."""
    e = tmp_path / "driveE"
    i = tmp_path / "driveI"
    lib = i / "Organized"
    for d in (e, i, lib, e / "Delete", i / "Delete"):
        d.mkdir(parents=True, exist_ok=True)

    def fake_drive_of(path: str) -> str:
        p = os.path.abspath(path)
        if p.startswith(str(e)):
            return "E"
        if p.startswith(str(i)):
            return "I"
        return "Z"

    store = Store.open(str(tmp_path / "ws" / ".mlo"))
    policy = PathPolicy(
        protected_substrings=("bluestacks",),
        blocked_drives=("C", "F"),
        staging_roots={"E": str(e / "Delete"), "I": str(i / "Delete")},
        library_root=str(lib),
        drive_of=fake_drive_of,
    )
    yield {
        "tmp": tmp_path, "E": e, "I": i, "lib": lib,
        "store": store, "policy": policy, "drive_of": fake_drive_of,
    }
    store.close()


@pytest.fixture()
def kernel(world):
    def make(execute: bool, run_id: str = "run-test", plan_id: str | None = None,
             disposer=None):
        return SafeOps(world["policy"], world["store"], run_id, execute, plan_id,
                       disposer=disposer)
    return make


def make_file(path: Path, content: bytes = b"hello world") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path
