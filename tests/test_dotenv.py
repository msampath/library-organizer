"""dotenv — minimal .env loader (P21/B8): standard KEY=value, env vars always
win over the file, missing file is normal not an error."""
from __future__ import annotations

import os

from mlo.dotenv import load_dotenv


def test_loads_simple_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("MLO_TEST_KEY", raising=False)
    p = tmp_path / ".env"
    p.write_text("MLO_TEST_KEY=abc123\n", encoding="utf-8")
    n = load_dotenv(str(p))
    assert n == 1
    assert os.environ["MLO_TEST_KEY"] == "abc123"


def test_skips_comments_and_blank_lines(tmp_path, monkeypatch):
    monkeypatch.delenv("MLO_A", raising=False)
    monkeypatch.delenv("MLO_B", raising=False)
    p = tmp_path / ".env"
    p.write_text("# a comment\n\nMLO_A=1\n   \nMLO_B=2\n", encoding="utf-8")
    n = load_dotenv(str(p))
    assert n == 2
    assert os.environ["MLO_A"] == "1" and os.environ["MLO_B"] == "2"


def test_strips_quotes(tmp_path, monkeypatch):
    monkeypatch.delenv("MLO_Q1", raising=False)
    monkeypatch.delenv("MLO_Q2", raising=False)
    p = tmp_path / ".env"
    p.write_text('MLO_Q1="double quoted"\nMLO_Q2=\'single quoted\'\n',
                encoding="utf-8")
    load_dotenv(str(p))
    assert os.environ["MLO_Q1"] == "double quoted"
    assert os.environ["MLO_Q2"] == "single quoted"


def test_existing_env_var_is_never_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv("MLO_EXISTING", "from-shell")
    p = tmp_path / ".env"
    p.write_text("MLO_EXISTING=from-file\n", encoding="utf-8")
    n = load_dotenv(str(p))
    assert n == 0                                # nothing NEWLY set
    assert os.environ["MLO_EXISTING"] == "from-shell"


def test_missing_file_returns_zero_not_an_error(tmp_path):
    assert load_dotenv(str(tmp_path / "nope.env")) == 0


def test_malformed_line_without_equals_is_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("MLO_OK", raising=False)
    p = tmp_path / ".env"
    p.write_text("this is not a valid line\nMLO_OK=yes\n", encoding="utf-8")
    n = load_dotenv(str(p))
    assert n == 1
    assert os.environ["MLO_OK"] == "yes"


def test_empty_key_is_skipped(tmp_path):
    p = tmp_path / ".env"
    p.write_text("=noKeyHere\n", encoding="utf-8")
    assert load_dotenv(str(p)) == 0
