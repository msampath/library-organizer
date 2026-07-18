"""End-to-end through the real CLI on a synthetic tree: init -> check -> scan ->
verdicts -> plan -> rehearse -> execute -> re-execute(no-op) -> verify.
Exercises the exit-code API the agent layer scripts against."""
from __future__ import annotations

import os
from pathlib import Path

from mlo.cli import main


def write(p: Path, content: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def build_world(tmp_path: Path) -> tuple[Path, Path, Path]:
    lib = tmp_path / "Organized"
    src = tmp_path / "old-drive"
    staging = tmp_path / "Delete"
    lib.mkdir()
    # library already holds one file; the source holds a duplicate of it,
    # a unique video, junk, and an unknown-extension file (REVIEW)
    write(lib / "Audio" / "kept.mp3", b"MUSIC" * 100)
    write(src / "backup" / "kept.mp3", b"MUSIC" * 100)          # ORGANIZED
    write(src / "camera" / "holiday.mp4", b"VIDEO" * 999)       # UNIQUE
    write(src / "junk" / "Thumbs.db", b"x")                     # JUNK
    write(src / "odd" / "data.xyzq", b"?" * 50)                 # REVIEW
    # P21/A4: on Windows key staging by the real drive letter (unchanged); on
    # POSIX (no drive-letter concept) key it by the common tmp_path PREFIX so
    # staging.root_for's longest-prefix match resolves it — no more fake "Z"
    # key that could never match a real path, no more Windows-only dedup leg.
    staging_key = (str(lib)[0].upper() if os.name == "nt"
                   else "'" + str(tmp_path) + "'")
    cfg = tmp_path / "mlo.toml"
    cfg.write_text(f'''
[library]
root = {str(lib)!r}
[[sources]]
name = "old-drive"
root = {str(src)!r}
[staging]
{staging_key} = {str(staging)!r}
[protected]
substrings = ["bluestacks"]
drives = []
[junk]
names = ["Thumbs.db"]
[classify]
max_unmatched_pct = 60.0
[taxonomy.buckets]
Video = [".mp4", ".mkv"]
Audio = [".mp3"]
''', encoding="utf-8")
    return cfg, lib, src


def run(cfg: Path, *argv: str) -> int:
    return main(["--config", str(cfg), *argv])


def test_full_lifecycle(tmp_path, capsys):
    cfg, lib, src = build_world(tmp_path)

    assert run(cfg, "check") == 0
    assert run(cfg, "scan", "library") == 0
    assert run(cfg, "scan", "old-drive") == 0
    assert run(cfg, "verdicts", "old-drive") == 0
    out = capsys.readouterr().out
    assert "ORGANIZED=1" in out and "UNIQUE=1" in out
    assert "JUNK=1" in out and "REVIEW=1" in out

    # organize: plan, rehearse, execute, idempotent re-execute
    assert run(cfg, "plan", "organize", "old-drive") == 0
    plan_path = next((tmp_path / ".mlo" / "plans").glob("plan-organize-*.jsonl"))
    assert run(cfg, "apply", str(plan_path)) == 0                 # rehearsal
    assert not (lib / "Video" / "old-drive" / "camera" / "holiday.mp4").exists()
    assert run(cfg, "apply", str(plan_path), "--execute") == 0
    assert (lib / "Video" / "old-drive" / "camera" / "holiday.mp4").exists()
    assert run(cfg, "apply", str(plan_path), "--execute") == 0    # pure no-op

    # dedup now allowed (organize executed); P21/A4 makes staging resolve on
    # both Windows (real drive letter) and POSIX (path-prefix key) — this leg
    # now actually exercises the disposal half on ubuntu-latest CI.
    assert run(cfg, "plan", "dedup", "old-drive") == 0
    dedup_path = next((tmp_path / ".mlo" / "plans").glob("plan-dedup-*.jsonl"))
    assert run(cfg, "apply", str(dedup_path), "--execute") == 0
    assert not (src / "backup" / "kept.mp3").exists()
    assert not (src / "junk" / "Thumbs.db").exists()
    assert (src / "odd" / "data.xyzq").exists()               # REVIEW stays
    assert (src / "camera" / "holiday.mp4").exists()          # copy, not move
    assert run(cfg, "verify", "staging") == 0

    assert run(cfg, "verify", "library") == 0
    assert run(cfg, "status") == 0
    assert run(cfg, "export", "ops") == 0


def test_undo_cli_wiring(tmp_path, capsys):
    """P21/C1: `mlo undo <run_id>` builds a plan through the CLI; an unknown
    run_id refuses cleanly. Full move_within reversal is unit-tested in
    tests/test_undo.py — this only exercises the CLI wiring."""
    cfg, lib, src = build_world(tmp_path)
    assert run(cfg, "check") == 0
    assert run(cfg, "scan", "library") == 0
    assert run(cfg, "scan", "old-drive") == 0
    assert run(cfg, "verdicts", "old-drive") == 0
    assert run(cfg, "plan", "organize", "old-drive") == 0
    plan_path = next((tmp_path / ".mlo" / "plans").glob("plan-organize-*.jsonl"))
    assert run(cfg, "apply", str(plan_path), "--execute") == 0
    out = capsys.readouterr().out
    summary_line = next(l for l in out.splitlines() if "summary:" in l)
    run_id = Path(summary_line.split("summary:", 1)[1].strip()).parent.name

    # organize places files via copy_in — undo has nothing reversible to do,
    # but must say so honestly rather than silently no-op.
    assert run(cfg, "undo", run_id) == 0
    out = capsys.readouterr().out
    assert "undo, 0 ops" in out
    assert "cannot be undone" in out

    assert run(cfg, "undo", "no-such-run") == 2


def test_verbose_flag_accepted_globally(tmp_path, capsys):
    """P21/C4: -v/--verbose parses on every command and changes nothing about
    exit codes; the per-file chatter it enables is unit-tested where it's
    produced (tests/test_hints.py, tests/test_apply.py)."""
    cfg, lib, src = build_world(tmp_path)
    assert main(["--config", str(cfg), "-v", "check"]) == 0
    assert main(["--config", str(cfg), "-v", "scan", "library"]) == 0
    assert main(["--config", str(cfg), "-v", "scan", "old-drive"]) == 0


def test_doctor_cli_wiring(tmp_path, capsys):
    """P21/C5: `mlo doctor` runs read-only, reports version/config/roots/store/
    last-run. Report-level cases (missing staging root, pending ops, etc.) are
    unit-tested in tests/test_doctor.py — this only exercises the CLI wiring."""
    cfg, lib, src = build_world(tmp_path)
    assert run(cfg, "doctor") == 0
    out = capsys.readouterr().out
    assert "mlo " in out
    assert "library: " in out and "[ok]" in out
    assert "source old-drive:" in out
    assert "store:" in out
    assert "last run: none yet" in out
    # build_world never creates the staging dir on disk — G349, the gap
    # doctor exists to close (config.validate never checks staging at all).
    assert "staging" in out and "[MISSING]" in out

    assert run(cfg, "scan", "library") == 0
    capsys.readouterr()
    assert run(cfg, "doctor") == 0
    out = capsys.readouterr().out
    assert "last run: scan (completed)" in out


def test_check_and_status_surface_pending_crash_recovery(tmp_path, capsys):
    """P21/C6: a leftover pending journal row is surfaced by `check`/`status`
    (and `doctor`, tested in test_doctor.py) — not just silently reconciled
    on the next execute."""
    from mlo.config import load
    from mlo.store import Store

    cfg, lib, src = build_world(tmp_path)
    assert run(cfg, "check") == 0
    assert "WARNING" not in capsys.readouterr().out

    ws = tmp_path / ".mlo"
    store = Store.open(str(ws))
    store.journal_intent("crashed-run", None, "op-x", "move_within",
                         str(lib / "a"), str(lib / "b"), None, None)
    store.close()

    assert run(cfg, "check") == 0
    out = capsys.readouterr().out
    assert "WARNING: 1 pending journal row(s)" in out

    assert run(cfg, "status") == 0
    out = capsys.readouterr().out
    assert "WARNING: 1 pending journal row(s)" in out


def test_dispose_cli_wiring(tmp_path, capsys):
    """P21/C2 — the L18 amendment: `mlo dispose` builds a plan over
    journal-explained staged content; executing it needs --confirm-dispose
    with the exact row count. This only exercises the build + refusal gate
    — actually EXECUTING would invoke the real OS disposer (Windows Recycle
    Bin / POSIX trash), which the kernel-level tests in test_dispose.py
    cover via an injected fake disposer instead."""
    cfg, lib, src = build_world(tmp_path)
    assert run(cfg, "check") == 0
    assert run(cfg, "scan", "library") == 0
    assert run(cfg, "scan", "old-drive") == 0
    assert run(cfg, "verdicts", "old-drive") == 0
    assert run(cfg, "plan", "organize", "old-drive") == 0
    org_path = next((tmp_path / ".mlo" / "plans").glob("plan-organize-*.jsonl"))
    assert run(cfg, "apply", str(org_path), "--execute") == 0
    assert run(cfg, "plan", "dedup", "old-drive") == 0
    dedup_path = next((tmp_path / ".mlo" / "plans").glob("plan-dedup-*.jsonl"))
    assert run(cfg, "apply", str(dedup_path), "--execute") == 0
    capsys.readouterr()

    assert run(cfg, "dispose") == 0
    out = capsys.readouterr().out
    assert "dispose, 1 ops" in out or "dispose, 2 ops" in out
    assert "--confirm-dispose" in out
    plan_line = next(l for l in out.splitlines() if l.startswith("plan "))
    plan_path = plan_line.split(":", 1)[1].strip()
    n_rows = int(plan_line.split(",")[1].strip().split()[0])

    assert run(cfg, "apply", plan_path, "--execute") == 2
    err = capsys.readouterr().err
    assert "confirm-dispose" in err

    assert run(cfg, "apply", plan_path, "--execute",
              "--confirm-dispose", str(n_rows + 1)) == 2
    capsys.readouterr()


def test_doctor_diagnoses_a_broken_setup_instead_of_refusing(tmp_path, capsys):
    """Super-review B-004: doctor exists to diagnose broken setups — an
    unreachable library root made _open()'s validate raise ConfigError before
    the doctor branch could ever run, so the one command that should report
    [MISSING] was itself exit 2."""
    import shutil

    cfg, lib, src = build_world(tmp_path)
    shutil.rmtree(lib)          # the library drive "went away"
    assert run(cfg, "check") == 2       # check still hard-refuses (L8)
    capsys.readouterr()
    assert run(cfg, "doctor") == 0      # doctor diagnoses instead
    out = capsys.readouterr().out
    assert "library: " in out and "[MISSING]" in out
    assert "config validation failed" in out


def test_exit_codes_are_api(tmp_path, capsys):
    cfg, lib, src = build_world(tmp_path)
    # 4: verdicts before any scan
    assert run(cfg, "verdicts", "old-drive") == 4
    # 2: dedup ordering gate (after scans + verdicts, uniques unorganized)
    assert run(cfg, "scan", "library") == 0
    assert run(cfg, "scan", "old-drive") == 0
    assert run(cfg, "verdicts", "old-drive") == 0
    assert run(cfg, "plan", "dedup", "old-drive") == 2
    # 2: unknown config key
    bad = tmp_path / "bad.toml"
    bad.write_text("[librray]\nroot='x'\n", encoding="utf-8")
    assert main(["--config", str(bad), "check"]) == 2
    # 5: coverage gate
    for i in range(40):
        write(src / "mass" / f"blob{i}.weird", bytes([i]) * 30)
    assert run(cfg, "scan", "old-drive") == 0
    assert run(cfg, "verdicts", "old-drive") == 0
    assert run(cfg, "plan", "organize", "old-drive") == 5


def test_sweep_holds_unique_then_stages_through_cli(tmp_path, capsys):
    """`mlo sweep` productizes the source-drive consolidation: it auto-scans the
    library, verdicts the source, HOLDS a source that still has UNIQUE (only-copy)
    files instead of touching it, and — once the uniques are preserved — stages
    the proven-in-library originals out with a 1 MiB confirm. Replaces the
    ad-hoc scan/verdict/plan/apply operator loop."""
    cfg, lib, src = build_world(tmp_path)

    # rehearsal, no prior scan: sweep scans the library itself, finds a UNIQUE
    # file, and HOLDS the source (exit 3) rather than sweeping around it
    assert run(cfg, "sweep", "old-drive") == 3
    out = capsys.readouterr().out
    assert "HELD" in out and "UNIQ=1" in out
    assert (src / "camera" / "holiday.mp4").exists()          # untouched

    # preserve the only-copy, then the sweep proceeds
    assert run(cfg, "plan", "organize", "old-drive") == 0
    plan_path = next((tmp_path / ".mlo" / "plans").glob("plan-organize-*.jsonl"))
    assert run(cfg, "apply", str(plan_path), "--execute") == 0

    # P21/A4: staging now resolves on POSIX too (path-prefix key), so this
    # leg is no longer Windows-only.
    assert run(cfg, "sweep", "old-drive", "--confirm-mb", "1",
               "--execute") == 0
    out = capsys.readouterr().out
    assert "swept" in out
    assert not (src / "backup" / "kept.mp3").exists()     # ORGANIZED -> Delete
    assert not (src / "junk" / "Thumbs.db").exists()      # JUNK -> Delete
    assert not (src / "camera" / "holiday.mp4").exists()  # now-organized -> Delete
    assert (src / "odd" / "data.xyzq").exists()           # REVIEW stays put
    assert (lib / "Video" / "old-drive" / "camera"
            / "holiday.mp4").exists()                     # preserved in library


def test_sweep_waive_organize_leaves_uniques_but_stages_dups(tmp_path, capsys):
    """`mlo sweep --waive-organize` sweeps a source's proven-in-library
    duplicates while LEAVING its UNIQUE (only-copy) files in place — for uniques
    that are a human-review pile (recovery carves), not library candidates."""
    cfg, lib, src = build_world(tmp_path)
    assert run(cfg, "scan", "library") == 0
    assert run(cfg, "sweep", "old-drive", "--waive-organize",
               "--confirm-mb", "1", "--execute") == 0
    out = capsys.readouterr().out
    assert "unique left in place" in out
    assert not (src / "backup" / "kept.mp3").exists()          # duplicate staged out
    assert not (src / "junk" / "Thumbs.db").exists()           # junk staged out
    assert (src / "camera" / "holiday.mp4").exists()           # UNIQUE left in place
    assert not (lib / "Video" / "old-drive" / "camera"
                / "holiday.mp4").exists()                      # never copied in


def test_check_skips_llm_preflight_when_disabled(tmp_path, capsys):
    """[llm] enabled = false (build_world's default) — check must not print
    an llm chain section at all (nothing to probe, no surprise network call)."""
    cfg, lib, src = build_world(tmp_path)
    assert run(cfg, "check") == 0
    out = capsys.readouterr().out
    assert "llm chain:" not in out


def test_check_runs_llm_preflight_when_enabled(tmp_path, capsys):
    """P21/B8: `check`'s help has always promised 'reachability' — the LLM
    chain is now actually probed when [llm] is on, informationally (a
    failure never fails `mlo check` itself)."""
    cfg, lib, src = build_world(tmp_path)
    text = cfg.read_text(encoding="utf-8")
    text += '\n[llm]\nenabled = true\nchain = ["local"]\n[llm.local]\nenabled = true\n'
    cfg.write_text(text, encoding="utf-8")
    assert run(cfg, "check") == 0                # a down local model never fails check
    out = capsys.readouterr().out
    assert "llm chain:" in out
    assert "local" in out


def test_identify_needs_exactly_one_of_source_or_review_set(tmp_path, capsys):
    """P21/B6 CLI wiring: argument validation happens before any LLM call —
    exit 2, not a crash, for neither-or-both."""
    cfg, lib, src = build_world(tmp_path)
    assert run(cfg, "scan", "library") == 0
    assert run(cfg, "identify") == 2
    err = capsys.readouterr().err
    assert "exactly one of --source or --review-set" in err


def test_identify_respects_llm_kill_switch(tmp_path):
    """[llm] enabled = false (build_world's default) — identify must refuse
    exactly like every other agent-layer command, never silently call out."""
    cfg, lib, src = build_world(tmp_path)
    assert run(cfg, "scan", "library") == 0
    assert run(cfg, "scan", "old-drive") == 0
    assert run(cfg, "verdicts", "old-drive") == 0
    assert run(cfg, "identify", "--source", "old-drive") == 2


def test_init_refuses_overwrite(tmp_path):
    cfg = tmp_path / "mlo.toml"
    assert main(["--config", str(cfg), "init"]) == 0
    assert main(["--config", str(cfg), "init"]) == 2


def test_agent_eval_mock_runs_through_cli(tmp_path, capsys):
    """The eval harness runs end-to-end via the CLI on the mock endpoint —
    no [llm] enabled, no live model — exactly what CI does."""
    cfg, lib, src = build_world(tmp_path)
    evals = Path(__file__).resolve().parent.parent / "evals"
    code = run(cfg, "agent", "eval", "--mock", "--dir", str(evals))
    out = capsys.readouterr().out
    assert code == 0                          # mock makes no dangerous errors
    assert '"task": "classify"' in out and '"task": "triage"' in out
    # P21/B7: the critic-panel eval runner now runs alongside classify/triage
    # whenever the golden set exists at <dir>/critics.json.
    assert '"task": "critics"' in out


def test_agent_eval_mock_skips_critics_when_golden_set_absent(tmp_path, capsys):
    """An older/custom --dir without critics.json must not crash — the row
    is simply omitted, matching the optional-golden-set posture."""
    cfg, lib, src = build_world(tmp_path)
    thin_evals = tmp_path / "thin-evals"
    thin_evals.mkdir()
    (thin_evals / "classify.json").write_text(
        '[{"path": "a.mp4", "gold": "Video"}]', encoding="utf-8")
    (thin_evals / "triage.json").write_text("[]", encoding="utf-8")
    code = run(cfg, "agent", "eval", "--mock", "--dir", str(thin_evals))
    out = capsys.readouterr().out
    assert code == 0
    assert '"task": "critics"' not in out


def test_agent_eval_live_respects_kill_switch(tmp_path):
    """A live eval with [llm] disabled must refuse (exit 2), not hammer a model."""
    cfg, lib, src = build_world(tmp_path)      # starter has [llm] enabled=false
    assert run(cfg, "agent", "eval") == 2


def test_stage_library_through_cli(tmp_path, capsys):
    """The library-staging path via the real CLI (a NameError here once
    slipped past builder-level tests): plan stage-library --paths -> execute
    stages the file and the index row goes with it."""
    import json
    cfg, lib, src = build_world(tmp_path)
    write(lib / "Other" / "dump" / "promo.mp4", b"VENDOR" * 200)
    assert run(cfg, "scan", "library") == 0
    listing = tmp_path / "stage-list.json"
    listing.write_text(json.dumps(["Other/dump/promo.mp4"]), encoding="utf-8")
    assert run(cfg, "plan", "stage-library", "--paths", str(listing),
               "--label", "triage") == 0
    plan_path = next((tmp_path / ".mlo" / "plans").glob("plan-stage-library-*.jsonl"))
    assert run(cfg, "apply", str(plan_path), "--execute") == 0
    staged = tmp_path / "Delete" / "triage" / "Other" / "dump" / "promo.mp4"
    assert staged.exists()
    assert not (lib / "Other" / "dump" / "promo.mp4").exists()


def test_reorganize_lifecycle_through_cli(tmp_path, capsys):
    """v0.2 repair flow via the real CLI: a flat v0.1-style library gets
    content-derived Jellyfin homes; hints route what filenames can't say;
    a second plan converges to zero; out-of-scope trees never move."""
    import json
    cfg, lib, src = build_world(tmp_path)
    # a flat mess like the one a v0.1 organize (or an older pipeline) leaves
    write(lib / "Video" / "old" / "films" / "Sivaji.The.Boss.(2007).mkv",
          b"MOVIE" * 200)
    write(lib / "Video" / "old" / "dash" / "FILE001.mp4", b"DASH" * 200)
    proper = lib / "Video" / "Movies" / "Tamil" / "Roja (1992)" / "Roja (1992).mkv"
    write(proper, b"ROJA" * 200)

    assert run(cfg, "scan", "library") == 0
    assert run(cfg, "plan", "reorganize", "--under", "Video/old") == 0
    out = capsys.readouterr().out
    plan_path = next((tmp_path / ".mlo" / "plans").glob("plan-reorganize-*.jsonl"))
    assert "no derivable identity" in out          # the dashcam clip

    assert run(cfg, "apply", str(plan_path), "--execute") == 0
    assert (lib / "Video" / "Movies" / "Other" / "Sivaji The Boss (2007)"
            / "Sivaji The Boss (2007).mkv").exists()
    assert (lib / "Video" / "old" / "dash" / "FILE001.mp4").exists()  # stayed
    assert proper.exists()                          # out of scope: untouched

    # hints route the dashcam clip to Personal; then the loop converges
    hints = tmp_path / "hints.json"
    hints.write_text(json.dumps({
        "Video/old/dash/FILE001.mp4": {"media_kind": "personal"}}),
        encoding="utf-8")
    assert run(cfg, "plan", "reorganize", "--under", "Video/old",
               "--hints", str(hints)) == 0
    plan2 = sorted((tmp_path / ".mlo" / "plans").glob("plan-reorganize-*.jsonl"),
                   key=lambda p: p.stat().st_mtime)[-1]
    assert run(cfg, "apply", str(plan2), "--execute") == 0
    # the dash/ grouping folder is preserved under Personal, not flattened
    assert (lib / "Video" / "Personal" / "dash" / "FILE001.mp4").exists()

    assert run(cfg, "plan", "reorganize", "--under", "Video/old",
               "--hints", str(hints)) == 0
    out = capsys.readouterr().out
    assert "(reorganize, 0 ops)" in out             # converged
