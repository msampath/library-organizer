"""P15 super-review hardening sweep — regression pins for the fixes.

Each test traces to a validator-confirmed finding from the 2026-07-13
super-review (ledger C39-C41). The C39/C40/C41 mechanism tests
live in test_containers.py / test_p13_features.py; this file pins the
store/apply/config/agent/taxonomy corrections.
"""
from __future__ import annotations

import os

import pytest

from conftest import make_file
from helpers import make_cfg
from mlo import apply as applymod, containers, fingerprint
from mlo.agent import protocol
from mlo.audioclass import song_bucket
from mlo.config import ConfigError, load
from mlo.store import Store
from mlo.taxonomy import route

TAX = {"Video": (".mp4", ".mkv"), "Audio": (".mp3",),
       "Photos": (".jpg",), "Documents": (".pdf", ".txt")}


# ── store ────────────────────────────────────────────────────────────────────

def test_journal_intent_retry_refreshes_pre_fingerprint(world):
    """A drift-skip journals pre as NULL; the retry must restore the real pre
    so the L2 crash-reconcile hash check works on the retried op."""
    st = world["store"]
    st.journal_intent("r1", None, "op-x", "move_within", "a", "b", None, None)
    st.complete_op("op-x", "skipped_drift", "content drift")
    st.journal_intent("r2", None, "op-x", "move_within", "a", "b", 123, "qh")
    ops = {o["op_id"]: o for o in st.pending_ops()}
    assert ops["op-x"]["pre_size"] == 123
    assert ops["op-x"]["pre_quick_hash"] == "qh"


def test_complete_op_never_demotes_done(world):
    """A racing second writer must not overwrite a terminal 'done'."""
    st = world["store"]
    st.journal_intent("r1", None, "op-y", "move_within", "a", "b", 1, "q")
    st.complete_op("op-y", "done", "ok")
    st.complete_op("op-y", "failed", "racer")
    assert st.op_state("op-y") == "done"


def test_corrupt_state_db_raises_config_error_naming_backups(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "state.db").write_bytes(b"NOT A SQLITE DATABASE AT ALL" * 10)
    with pytest.raises(ConfigError) as e:
        Store.open(str(ws))
    assert "backups" in str(e.value)


def test_schema_version_stamped_and_newer_refused(tmp_path):
    st = Store.open(str(tmp_path / "ws"))
    ver = st._con.execute("PRAGMA user_version").fetchone()[0]
    st.close()
    assert ver == Store.SCHEMA_VERSION
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "ws" / "state.db"))
    con.execute(f"PRAGMA user_version={Store.SCHEMA_VERSION + 1}")
    con.commit()
    con.close()
    with pytest.raises(ConfigError):
        Store.open(str(tmp_path / "ws"))


# ── apply: crash reconcile + audit ───────────────────────────────────────────

def test_reconcile_rmdir_empty_done_and_retryable(world, tmp_path):
    """rmdir journals src == dst; gone dir = done, present dir = retryable —
    not 'both ends missing' / hand-resolve AMBIGUOUS."""
    st = world["store"]
    gone = str(tmp_path / "gone-dir")
    present = str(tmp_path / "present-dir")
    os.makedirs(present)
    st.journal_intent("r1", None, "op-rm1", "rmdir_empty", gone, gone,
                      None, None)
    st.journal_intent("r1", None, "op-rm2", "rmdir_empty", present, present,
                      None, None)
    applymod.reconcile_pending(st, str(world["lib"]))
    assert st.op_state("op-rm1") == "done"
    assert st.op_state("op-rm2") == "failed"        # retryable, not ambiguous
    row = st._con.execute(
        "SELECT detail FROM ops WHERE op_id='op-rm2'").fetchone()
    assert "retryable" in row[0]


# ── config: refuse loudly, never crash raw ───────────────────────────────────

def _write_cfg(tmp_path, extra: str) -> str:
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    p = tmp_path / "mlo.toml"
    p.write_text(
        f"[library]\nroot = '{lib}'\n"
        "[taxonomy.buckets]\nVideo = ['.mp4']\n" + extra,
        encoding="utf-8")
    return str(p)


def test_llm_chain_as_string_refused(tmp_path):
    p = _write_cfg(tmp_path, "[llm]\nenabled = true\nchain = 'local'\n")
    with pytest.raises(ConfigError) as e:
        load(p)
    assert "array" in str(e.value)


def test_wrong_typed_section_refused(tmp_path):
    p = _write_cfg(tmp_path, "junk = 'zero'\n")
    with pytest.raises(ConfigError):
        load(p)


def test_non_numeric_local_num_ctx_refused(tmp_path):
    p = _write_cfg(tmp_path,
                   "[llm]\nenabled = true\n[llm.local]\nnum_ctx = 'big'\n")
    with pytest.raises(ConfigError) as e:
        load(p)
    assert "num_ctx" in str(e.value)


def test_image_patterns_surface_parses_and_refuses_bad_kind(tmp_path):
    p = _write_cfg(tmp_path,
                   "[classify.image_patterns]\nui = ['(?i)^sprite']\n")
    cfg = load(p)
    assert cfg.image_patterns == {"ui": ("(?i)^sprite",)}
    p2 = _write_cfg(tmp_path, "[classify.image_patterns]\nbogus = ['x']\n")
    with pytest.raises(ConfigError):
        load(p2)


# ── agent: escalation + temperature ──────────────────────────────────────────

class _CloudOnlyClient:
    """Fake duck-typed client: cloud-only chain, always-invalid replies."""
    def __init__(self):
        self.calls = 0

    def has_local(self):
        return False

    def complete(self, system, user, *, tier="any", max_tokens=2048):
        self.calls += 1
        class R:
            text = "not json at all"
            ledger = [{"entry": "cloud", "outcome": "ok"}]
            entry = "cloud"
        return R()


def test_run_task_does_not_rebuy_cloud_on_escalation():
    """On a chain with no enabled local entry, the 'strong' pass would re-buy
    the identical cloud model — run_task gives up after the base tier."""
    spec = protocol.TaskSpec(name="t", system="s")
    client = _CloudOnlyClient()
    out = protocol.run_task(client, spec, "user")
    assert out.value is None and out.unsure
    assert client.calls == 2                 # first + repair, NO strong re-buy


def test_anthropic_adapter_pins_temperature(monkeypatch, world):
    from mlo.agent import llm as llmmod
    captured = {}

    def transport(url, payload, headers, timeout):
        captured.update(payload)
        return {"content": [{"text": "{}"}]}

    monkeypatch.setenv("MLO_ANTHROPIC_KEY", "k")
    cfg = make_cfg(world, taxonomy=TAX)
    import dataclasses
    cfg = dataclasses.replace(cfg, llm=dataclasses.replace(
        cfg.llm, enabled=True, chain=("claude-x",)))
    client = llmmod.ChainClient(cfg, transport=transport)
    client._call_entry("claude-x", "sys", "usr", 100)
    assert captured.get("temperature") == 0.2


# ── taxonomy: drain-leak + year-correction depth guard ───────────────────────

def test_song_directly_under_media_top_does_not_leak_top_label(world):
    """A song at depth 2 (Audio\\song.mp3) routes FLAT — the media-top label
    must not ride along as a phantom 'album' folder under the language."""
    cfg = make_cfg(world, taxonomy=TAX)
    r = route(cfg, os.sep.join(["Audio", "Kadhal Rojave Tamil.mp3"]))
    assert r is not None
    assert r.dest_relpath == os.sep.join(
        ["Audio", "Music", "Tamil", "Kadhal Rojave Tamil.mp3"])


def test_exif_year_correction_leaves_album_subfolders_alone(world):
    """A photo inside '2013/Wedding Album/' is the human's grouping — a
    different EXIF year must not tear the album apart file by file."""
    from mlo.taxonomy import Hints
    cfg = make_cfg(world, taxonomy=TAX)
    rel = os.sep.join(["Images", "Photos", "2013", "Wedding Album", "x.jpg"])
    r = route(cfg, rel, Hints(year=2014))
    assert r.rule == "route:photo:already-placed"
    # directly under the year dir, the correction still applies
    rel2 = os.sep.join(["Images", "Photos", "2013", "x.jpg"])
    r2 = route(cfg, rel2, Hints(year=2014))
    assert r2.rule == "route:photo:exif-year-correction"


# ── classifiers: extension seams + false positives ───────────────────────────

def test_song_bucket_honours_user_devotional_patterns():
    assert song_bucket("Some Padam Recording.mp3") is None
    assert song_bucket("Some Padam Recording.mp3",
                       {"devotional": (r"\bpadam\b",)}) == "devotional"


def test_provenance_seg_spares_thumbnails():
    assert containers.PROVENANCE_SEG.search("Thumbnails") is None
    assert containers.PROVENANCE_SEG.search("OldThumbDrive") is not None


def test_albumart_prefix_requires_boundary(world):
    from mlo.plan import _is_sidecar_of
    # AlbumArt_{GUID}_Large.jpg IS an accessory
    assert _is_sidecar_of("AlbumArt_{X}_Large.jpg", "AlbumArt_{X}_Large",
                          "Some Song")
    # 'AlbumArtist - Track.mp3' is NOT
    assert not _is_sidecar_of("AlbumArtist - Track.mp3", "AlbumArtist - Track",
                              "Some Song")


# ── enrich: evidence honours audio_patterns ──────────────────────────────────

def test_compose_query_respects_config_audio_patterns(world):
    from mlo.enrich import evidence
    cfg = make_cfg(world, taxonomy=TAX)
    item = {"relpath": "Audio\\dump\\Great Stage Show Live.mp3",
            "bucket": "Audio", "size": 1}
    assert evidence.compose_query(item, cfg) is not None
    import dataclasses
    cfg2 = dataclasses.replace(
        cfg, audio_patterns={"junk": (r"(?i)stage show",)})
    assert evidence.compose_query(item, cfg2) is None
