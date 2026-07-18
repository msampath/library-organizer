"""W5-U2: the onboarding interview generates a valid, checkable mlo.toml from the
parameterized config surface."""
from __future__ import annotations

from mlo import interview
from mlo.config import load, validate


def test_build_config_toml_is_valid_and_checkable(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    answers = {
        "library_root": str(lib),
        "sources": [{"name": "old", "root": str(src)}],
        "sacrosanct": ["bluestacks"],          # "never touch BlueStacks" -> an answer
        "off_limits_drives": [],
        "languages": {"English": ["english"], "Tamil": ["tamil"]},
        "local_model": "gpt-oss:20b",
    }
    text = interview.build_config_toml(answers)
    p = tmp_path / "mlo.toml"
    p.write_text(text, encoding="utf-8")

    cfg = load(str(p))                          # structurally valid (unknown-key clean)
    assert cfg.library_root == str(lib)
    assert cfg.source("old").root == str(src)
    assert "bluestacks" in cfg.protected_substrings
    assert "Tamil" in cfg.layout.languages
    assert cfg.llm.local.model == "gpt-oss:20b"
    # the static Jellyfin defaults render and load (taxonomy + finer subtypes)
    assert ".mkv" in cfg.taxonomy["Video"]
    assert cfg.layout.subtypes["whatsapp"] == "Video/WhatsApp"

    # `mlo check` (filesystem validation) passes with a workspace outside the roots
    notes = validate(cfg, str(tmp_path / ".mlo"))
    assert all(isinstance(n, str) for n in notes)   # no ConfigError raised


def test_run_interview_collects_answers():
    scripted = iter([
        "X:/Organized",     # library root
        "E:/",              # source root
        "old-drive",        # source name
        "",                 # blank -> finish sources
        "bluestacks",       # sacrosanct
        "C",                # off-limits drive
        "English, Tamil",   # languages
        "gpt-oss:20b",      # local model
    ])
    out = interview.run_interview(input_fn=lambda _prompt: next(scripted),
                                  print_fn=lambda *a, **k: None)
    assert out["library_root"] == "X:/Organized"
    assert out["sources"] == [{"name": "old-drive", "root": "E:/"}]
    assert out["sacrosanct"] == ["bluestacks"]
    assert out["off_limits_drives"] == ["C"]
    assert set(out["languages"]) == {"English", "Tamil"}
    assert out["local_model"] == "gpt-oss:20b"


def test_generated_config_rejects_a_quote_injection():
    import pytest
    with pytest.raises(ValueError):
        interview.build_config_toml({"library_root": "X:/Org'ized"})


def test_staging_for_unc_source_produces_loadable_config(tmp_path):
    """P21/A4: a UNC source root must not produce the invalid
    '\\\\SERVER\\SHARE:\\Delete' string (illegal as a bare TOML key AND an
    illegal Windows path) — it gets a real, loadable staging entry instead."""
    lib = tmp_path / "lib"
    lib.mkdir()
    answers = {
        "library_root": str(lib),
        "sources": [{"name": "nas", "root": r"\\FAKESERVER\Media"}],
        "sacrosanct": [], "off_limits_drives": [],
        "languages": {"English": ["english"]},
        "local_model": "gpt-oss:20b",
    }
    text = interview.build_config_toml(answers)
    assert r"\\FAKESERVER\Media:\Delete" not in text     # the exact old bug
    p = tmp_path / "mlo.toml"
    p.write_text(text, encoding="utf-8")
    cfg = load(str(p))                                    # must not raise
    assert any("FAKESERVER" in k for k in cfg.staging)
    # (not calling validate(): \\FAKESERVER\Media is a fake, unreachable UNC
    # path by design — this test is about structural loadability + key shape)


def test_staging_for_posix_style_source_is_not_silently_empty(
        tmp_path, monkeypatch):
    """P21/A4: on a host where drive_of() can't identify a root at all (the
    real POSIX case — simulated here via monkeypatch since drive_of is
    OS-native), staging must key by the root's own path rather than silently
    producing an empty [staging] table."""
    monkeypatch.setattr(interview.winpath, "drive_of", lambda p: "")
    lib = tmp_path / "lib"
    lib.mkdir()
    posix_source = "/mnt/media"
    staging = interview._staging_for(
        str(lib), [{"name": "nas", "root": posix_source}], None)
    assert staging                              # never silently empty
    assert any(posix_source in k or k in posix_source for k in staging)


def test_multiword_language_and_quoted_model_render_loadable_toml(tmp_path):
    """Super-review M9: free-text language names and model strings must pass
    through the TOML escaping helpers — 'Sri Lankan' as a bare key or a quote
    in the model string used to render an unloadable config."""
    import tomllib

    from mlo.interview import build_config_toml

    import pytest

    answers = {
        "library_root": str(tmp_path / "Organized"),
        "sources": [{"name": "src", "root": str(tmp_path / "src")}],
        "staging": {"C": str(tmp_path / "Delete")},
        "sacrosanct": [], "off_limits": [],
        "junk_names": [], "junk_exts": [],
        "languages": {"Sri Lankan": ["sinhala"], "English": ["english"]},
        "local_model": "gpt-oss:20b",
    }
    text = build_config_toml(answers)
    parsed = tomllib.loads(text)          # must not raise
    assert parsed["layout"]["languages"]["Sri Lankan"] == ["sinhala"]
    assert parsed["llm"]["local"]["model"] == "gpt-oss:20b"

    # a quote in the free-text model answer refuses LOUDLY (the _basic
    # posture) instead of silently emitting unparseable TOML
    answers["local_model"] = 'weird"model'
    with pytest.raises(ValueError, match="double quote"):
        build_config_toml(answers)


def test_toml_key_quotes_non_bare_safe_keys():
    assert interview._toml_key("E") == "E"
    assert interview._toml_key(r"\\NAS\Share") == r"'\\NAS\Share'"
    assert interview._toml_key("/mnt/media") == "'/mnt/media'"
    import pytest
    with pytest.raises(ValueError):
        interview._toml_key("has'quote")
