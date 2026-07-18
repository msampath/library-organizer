"""Config validation (defects L6, L8)."""
from __future__ import annotations

import pytest

from mlo import config as cfgmod
from mlo.config import ConfigError


def write_cfg(tmp_path, body: str):
    p = tmp_path / "mlo.toml"
    p.write_text(body, encoding="utf-8")
    return str(p)


def minimal(tmp_path, extra: str = "", lib: str | None = None) -> str:
    lib_dir = lib or str(tmp_path / "lib")
    (tmp_path / "lib").mkdir(exist_ok=True)
    return write_cfg(tmp_path, f'''
[library]
root = {lib_dir!r}
{extra}
''')


def test_minimal_loads(tmp_path):
    cfg = cfgmod.load(minimal(tmp_path))
    assert cfg.library_root.endswith("lib")
    assert cfg.max_unmatched_pct == 5.0
    assert cfg.config_hash


def test_unknown_top_level_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown config keys.*librray"):
        cfgmod.load(write_cfg(tmp_path, '[librray]\nroot = "x"\n'))


def test_unknown_nested_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match="classify.max_unmatched_pct_typo"):
        cfgmod.load(minimal(tmp_path, "[classify]\nmax_unmatched_pct_typo = 3\n"))


def test_unknown_source_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"sources\[0\].enalbed"):
        cfgmod.load(minimal(
            tmp_path, '[[sources]]\nname = "e"\nroot = "x"\nenalbed = true\n'))


def test_duplicate_source_name_rejected(tmp_path):
    with pytest.raises(ConfigError, match="duplicate source name"):
        cfgmod.load(minimal(
            tmp_path,
            '[[sources]]\nname = "e"\nroot = "x"\n'
            '[[sources]]\nname = "e"\nroot = "y"\n'))


def test_name_patterns_load_and_validate(tmp_path):
    cfg = cfgmod.load(minimal(
        tmp_path,
        '[classify.name_patterns]\njunk = ["^Introducing Seagate "]\n'))
    assert cfg.name_patterns == {"junk": ("^Introducing Seagate ",)}
    with pytest.raises(ConfigError, match="unknown kind 'malware'"):
        cfgmod.load(minimal(
            tmp_path, '[classify.name_patterns]\nmalware = ["^x"]\n'))
    with pytest.raises(ConfigError, match="bad regex"):
        cfgmod.load(minimal(
            tmp_path, '[classify.name_patterns]\njunk = ["[unclosed"]\n'))


def test_audio_patterns_load_and_validate(tmp_path):
    cfg = cfgmod.load(minimal(
        tmp_path,
        '[classify.audio_patterns]\ncomedy = ["S Ve.? Shekher"]\n'))
    assert cfg.audio_patterns == {"comedy": ("S Ve.? Shekher",)}
    with pytest.raises(ConfigError, match="unknown kind 'song'"):
        cfgmod.load(minimal(
            tmp_path, '[classify.audio_patterns]\nsong = ["^x"]\n'))
    with pytest.raises(ConfigError, match="bad regex"):
        cfgmod.load(minimal(
            tmp_path, '[classify.audio_patterns]\ncomedy = ["[unclosed"]\n'))


def test_missing_library_root(tmp_path):
    with pytest.raises(ConfigError, match="library.root"):
        cfgmod.load(write_cfg(tmp_path, "[classify]\nmax_unmatched_pct = 2\n"))


def test_taxonomy_extensions_must_be_dotted(tmp_path):
    with pytest.raises(ConfigError, match="must start with '.'"):
        cfgmod.load(minimal(tmp_path, '[taxonomy.buckets]\nVideo = ["mp4"]\n'))


def test_validate_unreachable_enabled_source(tmp_path):
    cfg = cfgmod.load(minimal(
        tmp_path,
        f'[[sources]]\nname = "dead"\nroot = {str(tmp_path / "nope")!r}\n'))
    with pytest.raises(ConfigError, match="remedies.*enabled = false"):
        cfgmod.validate(cfg, str(tmp_path / "ws"))


def test_validate_disabled_source_is_note_not_error(tmp_path):
    cfg = cfgmod.load(minimal(
        tmp_path,
        f'[[sources]]\nname = "dead"\nroot = {str(tmp_path / "nope")!r}\n'
        'enabled = false\n'))
    notes = cfgmod.validate(cfg, str(tmp_path / "ws"))
    assert any("disabled" in n for n in notes)


def test_validate_workspace_must_not_live_under_roots(tmp_path):
    cfg = cfgmod.load(minimal(tmp_path))
    with pytest.raises(ConfigError, match="workspace"):
        cfgmod.validate(cfg, str(tmp_path / "lib" / ".mlo"))


def test_source_lookup(tmp_path):
    cfg = cfgmod.load(minimal(
        tmp_path, f'[[sources]]\nname = "e"\nroot = {str(tmp_path / "lib")!r}\n'))
    assert cfg.source("e").name == "e"
    with pytest.raises(ConfigError, match="unknown source"):
        cfg.source("zzz")


def test_llm_section_parses(tmp_path):
    cfg = cfgmod.load(minimal(tmp_path, '''
[llm]
enabled = true
chain = ["local", "claude-haiku-4-5"]
[llm.local]
enabled = true
model = "gpt-oss:20b"
num_ctx = 4096
'''))
    assert cfg.llm.enabled and cfg.llm.chain == ("local", "claude-haiku-4-5")
    assert cfg.llm.local.model == "gpt-oss:20b"
    assert cfg.llm.local.num_ctx == 4096
    assert cfg.llm.local.timeout_s == 240   # default preserved


def test_comics_root_default_and_override(tmp_path):
    assert cfgmod.load(minimal(tmp_path, "")).layout.comics_root == "Comics"
    cfg = cfgmod.load(minimal(tmp_path, '[layout]\ncomics_root = "Books/Comics"\n'))
    assert cfg.layout.comics_root == "Books/Comics"


def test_staging_drive_letter_key_still_works(tmp_path):
    body = "[staging]\n" + r"E = 'E:\Delete-mlo'" + "\n"
    cfg = cfgmod.load(minimal(tmp_path, body))
    assert cfg.staging == {"E": r"E:\Delete-mlo"}


def test_staging_unc_share_key_accepted(tmp_path):
    """P21/A4: a UNC share is now a valid staging key (the B1 blocker)."""
    body = "[staging]\n" + r"'\\NAS\Share' = '\\NAS\Share\Delete-mlo'" + "\n"
    cfg = cfgmod.load(minimal(tmp_path, body))
    assert r"\\NAS\Share" in cfg.staging
    assert cfg.staging[r"\\NAS\Share"] == r"\\NAS\Share\Delete-mlo"


def test_staging_posix_mount_key_accepted_and_not_case_folded(tmp_path):
    """P21/A4: an absolute POSIX path key is accepted verbatim — case is NOT
    folded (POSIX filesystems are case-sensitive; uppercasing would silently
    break matching)."""
    cfg = cfgmod.load(minimal(
        tmp_path, "[staging]\n'/mnt/Media' = '/mnt/Media/Delete-mlo'\n"))
    assert cfg.staging == {"/mnt/Media": "/mnt/Media/Delete-mlo"}


def test_staging_invalid_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match="staging keys must be"):
        cfgmod.load(minimal(tmp_path, "[staging]\nrelative_path = 'x'\n"))


def test_staging_two_letter_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match="staging keys must be"):
        cfgmod.load(minimal(tmp_path, "[staging]\nEE = 'x'\n"))


def test_validate_skips_drive_consistency_check_for_prefix_keys(tmp_path):
    """A UNC/POSIX prefix key has no single drive_of() identity to
    cross-check against — validate() must not falsely reject it."""
    cfg = cfgmod.load(minimal(
        tmp_path,
        "[staging]\n'/mnt/anything' = '/some/unrelated/Delete-mlo'\n"))
    notes = cfgmod.validate(cfg, str(tmp_path / "ws"))
    assert all(isinstance(n, str) for n in notes)   # no ConfigError raised


def test_enrich_section_defaults_off(tmp_path):
    cfg = cfgmod.load(minimal(tmp_path))
    assert cfg.enrich.searxng_url == ""
    assert cfg.enrich.tmdb_enabled is False
    assert cfg.enrich.id3_enabled is False
    assert cfg.enrich.opensubtitles_enabled is False


def test_enrich_section_parses(tmp_path):
    cfg = cfgmod.load(minimal(tmp_path, '''
[enrich]
searxng_url = "http://localhost:8080"
tmdb_enabled = true
id3_enabled = true
'''))
    assert cfg.enrich.searxng_url == "http://localhost:8080"
    assert cfg.enrich.tmdb_enabled is True
    assert cfg.enrich.id3_enabled is True
    assert cfg.enrich.opensubtitles_enabled is False


def test_enrich_rejects_unknown_key_and_no_api_keys(tmp_path):
    """API keys must never be accepted in mlo.toml (owner directive) — only
    env vars. A 'tmdb_key' key is an unknown-key rejection, not a feature."""
    with pytest.raises(ConfigError, match="enrich.tmdb_key"):
        cfgmod.load(minimal(
            tmp_path, '[enrich]\ntmdb_key = "secret"\n'))


def test_llm_critics_chain_parses(tmp_path):
    cfg = cfgmod.load(minimal(tmp_path, '''
[llm]
enabled = true
chain = ["local"]
critics_chain = ["claude-opus-4-8", "local"]
'''))
    assert cfg.llm.critics_chain == ("claude-opus-4-8", "local")
    assert cfg.llm.chain == ("local",)
    with pytest.raises(ConfigError, match="llm.critics_chian"):
        cfgmod.load(minimal(tmp_path, '[llm]\ncritics_chian = ["x"]\n'))
