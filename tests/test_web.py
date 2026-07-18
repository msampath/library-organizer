"""The web UI drives the SAME safe engine as the CLI. These tests exercise the
pure action functions (act_setup -> scan -> verdicts -> plan -> rehearse ->
execute -> verify) end-to-end on a synthetic tree, plus the two things unique to
the UI: the generated-config round-trip and the folder-structure preview.

The 2-pass (pilot) tests below drive the same handlers the browser calls:
analyze as a background job, the sealed proposal + row drill-down routes, and
execute gated by hash-bound approvals — everything through a REAL mlo.toml on
disk, because that is what the web layer loads (no injected Config objects).
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from mlo import web
from mlo.config import load, validate
from mlo.report import PlanIntegrityError


def write(p: Path, content: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def build_world(tmp_path: Path) -> tuple[str, Path, Path]:
    lib = tmp_path / "Organized"
    src = tmp_path / "old-drive"
    lib.mkdir()
    write(lib / "Audio" / "kept.mp3", b"MUSIC" * 100)
    write(src / "backup" / "kept.mp3", b"MUSIC" * 100)        # ORGANIZED (dup)
    write(src / "camera" / "holiday.mp4", b"VIDEO" * 999)     # UNIQUE
    write(src / "photos" / "pic.jpg", b"IMG" * 200)           # UNIQUE
    write(src / "docs" / "readme.pdf", b"DOC" * 50)           # UNIQUE
    return str(tmp_path / "mlo.toml"), lib, src


def setup(cfg_path: str, lib: Path, src: Path) -> dict:
    return web.act_setup(cfg_path, str(lib), "old-drive", str(src))


def test_boot_pending_warning_surfaces_a_crash_and_clears_after(tmp_path, capsys):
    """P21/C6: `mlo serve` boot detects a leftover pending journal row the
    same way `check`/`status`/`doctor` do, instead of only discovering it
    silently inside the next Execute's reconcile_pending call."""
    cfg_path, lib, src = build_world(tmp_path)
    r = setup(cfg_path, lib, src)
    assert r["ok"]

    assert web._boot_pending_warning(cfg_path) == 0
    assert capsys.readouterr().err == ""

    cfg, store = web._cfg_store(cfg_path)
    store.journal_intent("crashed-run", None, "op-x", "move_within",
                         str(lib / "a"), str(lib / "b"), None, None)
    store.close()

    n = web._boot_pending_warning(cfg_path)
    assert n == 1
    assert "1 pending journal row(s)" in capsys.readouterr().err


def test_serve_boot_loads_workspace_dotenv(tmp_path, monkeypatch, capsys):
    """Super-review H4: `mlo serve` must load .mlo/.env like cli._open does —
    otherwise web-UI critic chains silently lose the keys the CLI has. The
    bind is stubbed to fail so serve() returns without running a server; the
    dotenv load happens before the bind."""
    cfg_path, lib, src = build_world(tmp_path)
    r = setup(cfg_path, lib, src)
    assert r["ok"]
    ws = Path(web._workspace(cfg_path))
    ws.mkdir(exist_ok=True)
    (ws / ".env").write_text("MLO_TEST_WEB_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv("MLO_TEST_WEB_KEY", raising=False)

    def boom(*a, **k):
        raise OSError("bind stubbed out")
    monkeypatch.setattr(web, "ThreadingHTTPServer", boom)
    import os as _os
    assert web.serve(cfg_path, port=1) == 2
    assert _os.environ.pop("MLO_TEST_WEB_KEY") == "from-dotenv"


def test_generated_config_round_trips(tmp_path):
    cfg_path, lib, src = build_world(tmp_path)
    r = setup(cfg_path, lib, src)
    assert r["ok"] and r["source_name"] == "old-drive"
    # The file it wrote is a valid config the engine accepts.
    cfg = load(cfg_path)
    validate(cfg, str(tmp_path / ".mlo"))
    assert cfg.library_root == str(lib)
    assert cfg.sources[0].root == str(src)


def test_full_pipeline_through_web_actions(tmp_path):
    cfg_path, lib, src = build_world(tmp_path)
    assert setup(cfg_path, lib, src)["ok"]

    assert web.act_scan(cfg_path, "library")["ok"]
    assert web.act_scan(cfg_path, "source")["count"] == 4

    v = web.act_verdicts(cfg_path)
    assert v["counts"]["ORGANIZED"] == 1
    assert v["counts"]["UNIQUE"] == 3
    assert v["counts"]["REVIEW"] == 0

    p = web.act_plan(cfg_path)
    assert p["ok"] and p["n_rows"] == 3
    # the preview tree is rooted at the library and carries file counts
    assert p["tree"]["name"] == "Organized" and p["tree"]["count"] == 3
    assert p["sample"], "expected sample moves for the user to eyeball"

    # rehearse touches nothing on disk
    assert web.act_apply(cfg_path, p["plan_path"], execute=False)["ok"]
    assert not (lib / "Video" / "old-drive" / "camera" / "holiday.mp4").exists()

    # execute copies the UNIQUE files in (originals kept — no delete)
    x = web.act_apply(cfg_path, p["plan_path"], execute=True)
    assert x["ok"] and x["exit_code"] == 0
    assert (lib / "Video" / "old-drive" / "camera" / "holiday.mp4").exists()
    assert (src / "camera" / "holiday.mp4").exists()          # source untouched

    verr = web.act_verify(cfg_path)
    assert verr["ok"] and not verr["blocking"]
    assert verr["counts"]["unindexed"] == 0


def test_state_reports_progress(tmp_path):
    cfg_path, lib, src = build_world(tmp_path)
    assert web.act_state(cfg_path)["config_exists"] is False
    setup(cfg_path, lib, src)
    st = web.act_state(cfg_path)
    assert st["config_exists"] and st["library_root"] == str(lib)
    assert st["index_fresh"] is False
    web.act_scan(cfg_path, "library")
    assert web.act_state(cfg_path)["index_fresh"] is True


def test_setup_refuses_missing_folder(tmp_path):
    cfg_path, lib, src = build_world(tmp_path)
    r = web.act_setup(cfg_path, str(lib), "old-drive", str(tmp_path / "nope"))
    assert not r["ok"] and "not found" in r["error"]


def test_setup_never_clobbers_hand_authored_config(tmp_path):
    cfg_path, lib, src = build_world(tmp_path)
    Path(cfg_path).write_text("[library]\nroot='x'\n", encoding="utf-8")
    r = setup(cfg_path, lib, src)
    assert not r["ok"] and "hand-authored" in r["error"]


def test_folder_tree_aggregates_counts_and_caps_width(tmp_path):
    lib = str(tmp_path / "Lib")
    dsts = [os.path.join(lib, "Video", "Movies", "a.mkv"),
            os.path.join(lib, "Video", "Movies", "b.mkv"),
            os.path.join(lib, "Images", "Photos", "c.jpg")]
    tree = web._folder_tree(dsts, lib)
    assert tree["count"] == 3
    names = {d["name"]: d for d in tree["dirs"]}
    assert names["Video"]["count"] == 2 and names["Images"]["count"] == 1

    wide = [os.path.join(lib, f"d{i}", "f.mp4") for i in range(60)]
    capped = web._folder_tree(wide, lib, max_children=40)
    assert len(capped["dirs"]) == 40 and capped["more"] == 20


# ── the 2-pass (pilot) flow through the web layer ─────────────────────────────

@pytest.fixture(autouse=True)
def _reset_pilot_web_state():
    """The job slot and the drive_of seam are module state — clear them so no
    test inherits another's job, and let a still-running worker drain before
    tmp dirs are torn down."""
    yield
    for _ in range(1200):
        with web._JOB_LOCK:
            done = web._JOB is None or web._JOB["finished"]
        if done:
            break
        time.sleep(0.05)
    with web._JOB_LOCK:
        web._JOB = None
    web._PILOT_DRIVE_OF = None
    web.Handler.session_token = None
    if web._ACTION_LOCK.locked():        # a refused-acquire path could strand it
        try:
            web._ACTION_LOCK.release()
        except RuntimeError:
            pass


def wait_job(timeout: float = 120.0) -> dict:
    """Poll the status route until the worker thread reports finished."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = web.act_pilot_status()["job"]
        if j and j["finished"]:
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


def build_pilot_world(tmp_path: Path, llm: bool = False) -> tuple[str, Path, Path, Path]:
    """test_pilot's little universe, but reachable through a REAL mlo.toml:
    a unique source movie, a source/library twin, junk, a misplaced library
    movie, and an unroutable dashcam clip. Staging is keyed to tmp_path's real
    drive letter (validate() checks it against the real winpath.drive_of); the
    web drive_of seam pins the same letter so same-drive staging rules hold on
    POSIX CI too, where paths carry no drive at all."""
    lib, src, stage = tmp_path / "Organized", tmp_path / "old-drive", tmp_path / "Delete"
    write(lib / "Audio" / "Music" / "Hindi" / "Album" / "track.mp3", b"TWIN-BYTES" * 40)
    write(lib / "Video" / "eSrc" / "films" / "Heat.(1995).mkv", b"HEAT" * 100)
    write(lib / "Video" / "eSrc" / "dash" / "FILE001.mp4", b"DASH" * 50)
    write(src / "films" / "Sivaji.The.Boss.(2007).mkv", b"SIVAJI" * 100)
    write(src / "music" / "track.mp3", b"TWIN-BYTES" * 40)
    write(src / "Thumbs.db", b"x")
    stage.mkdir()

    drv = os.path.splitdrive(str(tmp_path))[0].rstrip(":").upper() or "E"
    web._PILOT_DRIVE_OF = lambda p: drv
    llm_block = ""
    if llm:
        llm_block = ("[llm]\nenabled = true\nchain = [\"local\"]\n\n"
                     "[llm.local]\nenabled = true\n"
                     "url = 'http://localhost:1'\nmodel = 'scripted'\n")
    cfg_path = tmp_path / "mlo.toml"
    cfg_path.write_text(f"""\
[library]
root = '{lib}'

[[sources]]
name = "old-drive"
root = '{src}'
enabled = true

[staging]
{drv} = '{stage}'

[junk]
zero_byte = true
names = ["Thumbs.db"]

[classify]
max_unmatched_pct = 5.0

[taxonomy.buckets]
Video = [".mp4", ".mkv"]
Audio = [".mp3"]
Photos = [".jpg"]
Documents = [".pdf"]

{llm_block}""", encoding="utf-8")
    return str(cfg_path), lib, src, stage


def test_pilot_analyze_job_end_to_end(tmp_path):
    cfg_path, lib, src, stage = build_pilot_world(tmp_path)

    st = web.act_state(cfg_path)          # dashboard fields before anything ran
    assert st["ok"] and st["journal_pos"] == 0 and st["proposal"] is None
    assert st["latest_run"] is None
    assert st["sources"][0]["name"] == "old-drive"
    assert st["staging"], "staging roots should surface for the dashboard"
    assert not web.act_latest_summary(cfg_path)["ok"]

    r = web.act_pilot_analyze(cfg_path, {})
    assert r["ok"] and r["kind"] == "analyze" and r["job_id"]
    j = wait_job()
    assert j["error"] is None, j["error"]
    assert j["result"]["exit_code"] == 0

    phases = [e["phase"] for e in j["events"]]
    for expected in ("scan-library", "scan-source", "plan", "review-set",
                     "rehearse", "assemble"):
        assert expected in phases, f"missing progress phase {expected}"

    # Pass 1 is provably read-only: the ops journal never moved
    st = web.act_state(cfg_path)
    assert st["journal_pos"] == 0
    assert st["proposal"] is not None
    assert (src / "films" / "Sivaji.The.Boss.(2007).mkv").exists()

    # the sealed proposal comes back seal-verified, ids intact
    p = web.act_proposal(cfg_path)
    assert p["ok"]
    doc = p["proposal"]
    assert doc["proposal_sha256"] and doc["run"] == st["proposal"]["run"]
    ids = {s["id"]: s for s in doc["sections"]}
    assert ids["organize:old-drive"]["status"] == "ready"
    assert ids["dedup:old-drive"]["status"] == "gated"
    assert ids["reorganize:library"]["status"] == "ready"
    assert doc["execution_order"].index("organize:old-drive") \
        < doc["execution_order"].index("dedup:old-drive")


def test_second_job_is_refused_while_one_runs(tmp_path):
    cfg_path, *_ = build_pilot_world(tmp_path)
    gate = threading.Event()

    def hold(cfg, store, progress):
        progress("hold", {})
        assert gate.wait(30)
        return {"kind": "held"}

    assert web._job_start("analyze", cfg_path, hold)["ok"]
    try:
        r = web.act_pilot_analyze(cfg_path, {})
        assert not r["ok"] and r["error"] == "a job is already running"
    finally:
        gate.set()
    j = wait_job()
    assert j["result"] == {"kind": "held"}

    # a finished job frees the single slot
    assert web._job_start("analyze", cfg_path, lambda c, s, p: {"kind": "again"})["ok"]
    assert wait_job()["result"] == {"kind": "again"}


def test_tampered_proposal_is_refused(tmp_path):
    cfg_path, *_ = build_pilot_world(tmp_path)
    web.act_pilot_analyze(cfg_path, {})
    assert wait_job()["error"] is None
    run = web.act_state(cfg_path)["proposal"]["run"]
    ppath = os.path.join(os.path.dirname(cfg_path), ".mlo", "runs", run,
                         "proposal.json")
    raw = open(ppath, encoding="utf-8").read()
    assert '"max_cycles": 3' in raw
    open(ppath, "w", encoding="utf-8").write(
        raw.replace('"max_cycles": 3', '"max_cycles": 9'))
    with pytest.raises(PlanIntegrityError, match="hash mismatch"):
        web.act_proposal(cfg_path)
    with pytest.raises(PlanIntegrityError, match="hash mismatch"):
        web.act_proposal_rows(cfg_path, None, "reorganize:library", None, 0, 10)


def test_proposal_rows_pagination_cluster_filter_and_critic_join(tmp_path, monkeypatch):
    """The row drill-down: pages, filters by the executor's own cluster ids,
    and joins each row with its review-set signals and critic answer — the
    CANONICAL full-signal review surfaced to the human."""
    import mlo.agent.llm as llmmod
    from test_agent_protocol import scripted

    reply = {"media_kind": "personal", "language": "English", "year": None,
             "title": None, "proposed_home": "Video/Personal",
             "confidence": 0.9, "rationale": "dashcam numbering"}
    real = llmmod.ChainClient

    def scripted_client(cfg, transport=None):
        return real(cfg, transport=scripted([json.dumps(reply)]))

    monkeypatch.setattr(llmmod, "ChainClient", scripted_client)

    cfg_path, *_ = build_pilot_world(tmp_path, llm=True)
    web.act_pilot_analyze(cfg_path, {})
    j = wait_job()
    assert j["error"] is None, j["error"]

    doc = web.act_proposal(cfg_path)["proposal"]
    reorg = next(s for s in doc["sections"] if s["id"] == "reorganize:library")
    assert reorg["n_rows"] == 2          # Heat re-home + the hinted dashcam clip

    r1 = web.act_proposal_rows(cfg_path, None, "reorganize:library", None, 0, 1)
    r2 = web.act_proposal_rows(cfg_path, None, "reorganize:library", None, 1, 1)
    assert r1["ok"] and r1["total"] == 2 and len(r1["rows"]) == 1
    assert len(r2["rows"]) == 1
    assert r1["rows"][0]["op_id"] != r2["rows"][0]["op_id"]

    # the cluster filter partitions the section's rows exactly
    total = 0
    for c in reorg["clusters"]:
        rc = web.act_proposal_rows(cfg_path, None, "reorganize:library",
                                   c["id"], 0, 100)
        assert rc["total"] == c["n_rows"]
        total += rc["total"]
    assert total == reorg["n_rows"]
    assert web.act_proposal_rows(cfg_path, None, "reorganize:library",
                                 "no|such|cluster", 0, 100)["total"] == 0

    # the hinted row carries its review signals and the critic's full answer
    allr = web.act_proposal_rows(cfg_path, None, "reorganize:library", None, 0, 100)
    hinted = next(r for r in allr["rows"] if "FILE001" in r["src"])
    assert hinted["critic"]["proposed_home"] == "Video/Personal"
    assert hinted["critic"]["confidence"] == 0.9
    assert hinted["signals"] is not None and "siblings" in hinted["signals"]
    routed = next(r for r in allr["rows"] if "Heat" in r["src"])
    assert routed["critic"] is None      # routed by rule, not by a critic

    # unknown section and gated section behave honestly
    assert not web.act_proposal_rows(cfg_path, None, "nope:x", None, 0, 10)["ok"]
    gated = web.act_proposal_rows(cfg_path, None, "dedup:old-drive", None, 0, 10)
    assert gated["ok"] and gated["total"] == 0 and "Pass 2" in gated["note"]


def test_pilot_execute_via_web_moves_files_and_audits_approvals(tmp_path):
    cfg_path, lib, src, stage = build_pilot_world(tmp_path)
    web.act_pilot_analyze(cfg_path, {})
    assert wait_job()["error"] is None
    doc = web.act_proposal(cfg_path)["proposal"]

    # approvals shaped exactly as the UI builds them (approve-all here)
    decisions = {s["id"]: "approve" for s in doc["sections"]
                 if s["status"] in ("ready", "gated")}
    r = web.act_pilot_execute(cfg_path, {"run": doc["run"], "approvals": {
        "proposal_sha256": doc["proposal_sha256"],
        "decisions": decisions, "converge": True}})
    assert r["ok"] and r["kind"] == "execute"
    j = wait_job()
    assert j["error"] is None, j["error"]
    res = j["result"]
    assert res["exit_code"] == 0
    outs = {o["id"]: o for o in res["outcomes"]}
    assert outs["organize:old-drive"]["status"] == "converged"
    assert outs["dedup:old-drive"]["status"] == "converged"
    assert outs["reorganize:library"]["status"] == "converged"
    assert res["verify"]["blocking"] is False

    # the unique movie entered the library FIRST; the gated dedup then re-
    # verdicted the source copy as its twin and staged it (sweep semantics:
    # preserve, then stage the original — staged, never deleted); the twin
    # track and the junk staged too; the misplaced library movie re-homed
    assert (lib / "Video" / "Movies" / "Other" / "Sivaji The Boss (2007)"
            / "Sivaji The Boss (2007).mkv").exists()
    assert not (src / "films" / "Sivaji.The.Boss.(2007).mkv").exists()
    assert any(stage.rglob("Sivaji.The.Boss.(2007).mkv"))
    assert not (src / "music" / "track.mp3").exists()
    assert any(stage.rglob("track.mp3"))
    assert not (lib / "Video" / "eSrc" / "films" / "Heat.(1995).mkv").exists()

    # audit trail: the exact approvals landed in the run dir before execution,
    # and the summary is served by the route
    runs = Path(cfg_path).parent / ".mlo" / "runs"
    saved = [json.loads(p.read_text(encoding="utf-8"))
             for p in runs.glob("*/approvals.json")]
    assert any(a["proposal_sha256"] == doc["proposal_sha256"] for a in saved)
    latest = web.act_latest_summary(cfg_path)
    assert latest["ok"] and latest["summary"]["exit_code"] == 0


def test_execute_refuses_unbound_or_malformed_approvals(tmp_path):
    cfg_path, lib, src, stage = build_pilot_world(tmp_path)
    web.act_pilot_analyze(cfg_path, {})
    assert wait_job()["error"] is None

    # malformed: refused at submit, no job started
    r = web.act_pilot_execute(cfg_path, {"approvals": {"proposal_sha256": "x"}})
    assert not r["ok"] and "decisions" in r["error"]
    r = web.act_pilot_execute(cfg_path, {"approvals": {"decisions": {}}})
    assert not r["ok"] and "proposal_sha256" in r["error"]

    # stale binding: admitted, then refused by the engine (C25) — the job
    # surfaces the ApprovalsError text and nothing moves
    r = web.act_pilot_execute(cfg_path, {"approvals": {
        "proposal_sha256": "0" * 64, "decisions": {"organize:old-drive": "approve"}}})
    assert r["ok"]
    j = wait_job()
    assert j["error"] and "DIFFERENT proposal" in j["error"]
    assert (src / "music" / "track.mp3").exists()
    assert web.act_state(cfg_path)["journal_pos"] == 0


def test_http_smoke_real_server(tmp_path):
    """One real ThreadingHTTPServer on an ephemeral port: the page serves, the
    JSON routes answer, unknown routes 404, and the single-job rule holds over
    HTTP exactly as it does at the act layer."""
    cfg_path, *_ = build_pilot_world(tmp_path)
    prev = web.Handler.config_path
    web.Handler.config_path = cfg_path
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def req(method, path, body=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        payload = json.dumps(body) if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        c.request(method, path, payload, headers)
        resp = c.getresponse()
        data = resp.read()
        c.close()
        return resp.status, data

    try:
        code, page = req("GET", "/")
        assert code == 200 and b"<!doctype html>" in page
        assert b"Analyze" in page and b"Guided mode" in page

        code, data = req("GET", "/api/state")
        assert code == 200 and json.loads(data)["ok"] is True

        code, data = req("GET", "/api/proposal")
        assert code == 200 and "no proposal found" in json.loads(data)["error"]

        assert req("POST", "/api/nope", {})[0] == 404
        assert req("GET", "/api/nope")[0] == 404

        gate = threading.Event()

        def hold(cfg, store, progress):
            assert gate.wait(30)
            return {"kind": "held"}

        assert web._job_start("analyze", cfg_path, hold)["ok"]
        try:
            code, data = req("POST", "/api/pilot/analyze", {})
            out = json.loads(data)
            assert code == 200 and not out["ok"]
            assert out["error"] == "a job is already running"
            code, data = req("GET", "/api/pilot/status")
            assert json.loads(data)["job"]["kind"] == "analyze"
        finally:
            gate.set()
        wait_job()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_session_token_and_host_enforced(tmp_path):
    """With a session token set (as serve() does), every POST needs a matching
    X-MLO-Token and a loopback Host; a mutating action while a job holds the
    shared kernel-mutex is refused (409). Nothing here uses the bare-Handler
    dormant mode."""
    cfg_path, *_ = build_pilot_world(tmp_path)
    web.Handler.config_path = cfg_path
    web.Handler.session_token = "secret-token-xyz"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def req(method, path, body=None, headers=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        payload = json.dumps(body) if body is not None else None
        h = dict(headers or {})
        if payload:
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, payload, h)
        resp = c.getresponse()
        data = resp.read()
        c.close()
        return resp.status, data

    try:
        tok = {"X-MLO-Token": "secret-token-xyz"}
        # GET page still serves and embeds the token
        code, page = req("GET", "/")
        assert code == 200 and b"secret-token-xyz" in page

        # POST with no token -> 403; wrong token -> 403
        assert req("POST", "/api/scan", {"target": "library"})[0] == 403
        assert req("POST", "/api/scan", {"target": "library"},
                   {"X-MLO-Token": "wrong"})[0] == 403

        # POST with a non-loopback Host -> 403 (DNS-rebinding guard)
        assert req("POST", "/api/scan", {"target": "library"},
                   {**tok, "Host": "evil.example.com"})[0] == 403

        # correct token + loopback Host is admitted (any status but 403)
        assert req("POST", "/api/scan", {"target": "library"}, tok)[0] != 403

        # a mutating POST is refused (409) while a job holds the kernel-mutex
        gate = threading.Event()

        def hold(cfg, store, progress):
            assert gate.wait(30)
            return {"kind": "held"}

        assert web._job_start("analyze", cfg_path, hold)["ok"]
        try:
            code, _ = req("POST", "/api/apply",
                          {"plan_path": "x", "execute": True}, tok)
            assert code == 409
        finally:
            gate.set()
        wait_job()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_job_start_refused_while_action_lock_held(tmp_path):
    """The reverse-direction concurrency gap a review found: a pilot job must
    NOT be admitted while a synchronous mutating action holds the shared
    kernel-mutex — otherwise two kernels run in one process."""
    cfg_path, *_ = build_pilot_world(tmp_path)
    assert web._ACTION_LOCK.acquire(blocking=False)
    try:
        out = web._job_start("analyze", cfg_path, lambda c, s, p: {})
        assert out["ok"] is False
        assert "already running" in out["error"]
    finally:
        web._ACTION_LOCK.release()
