"""Property tests for path handling (defect L10)."""
from __future__ import annotations

import os

from hypothesis import given, strategies as st

from mlo import winpath

# Filename-ish text including lone surrogates (the Windows reality), excluding
# separators and NUL.
name_chars = st.characters(
    blacklist_characters="/\\\x00",
    min_codepoint=1, max_codepoint=0x10FFFF,
    blacklist_categories=(),
)
names = st.text(alphabet=name_chars, min_size=1, max_size=40)


@given(names)
def test_surrogatepass_roundtrip(name):
    path = os.path.join("some", "dir", name)
    assert winpath.from_bytes(winpath.to_bytes(path)) == os.path.abspath(path) \
        or winpath.from_bytes(winpath.to_bytes(path)) == path


@given(names)
def test_display_is_always_encodable(name):
    d = winpath.display(os.path.join("x", name))
    d.encode("utf-8")  # must never raise


def test_long_prefix_windows_only():
    p = winpath.to_long(os.path.join(os.getcwd(), "a", "b"))
    if os.name == "nt":
        assert p.startswith("\\\\?\\")
        assert winpath.from_long(p) == os.path.abspath(os.path.join(os.getcwd(), "a", "b"))
    else:
        assert not p.startswith("\\\\?\\")


def test_to_long_is_idempotent():
    p = winpath.to_long(os.getcwd())
    assert winpath.to_long(p) == p


def test_is_under_basics(tmp_path):
    root = tmp_path / "root"
    inside = root / "a" / "b.txt"
    sibling = tmp_path / "rootlike" / "c.txt"   # prefix-string trap
    assert winpath.is_under(str(inside), str(root))
    assert winpath.is_under(str(root), str(root))
    assert not winpath.is_under(str(sibling), str(root))
