"""Eval harness regression: metrics math + the mock endpoint end-to-end.
This is what CI runs instead of a live model; live numbers land in
docs/agent-design.md via `mlo agent eval` on real hardware."""
from __future__ import annotations

import json
from pathlib import Path

from helpers_plan import make_cfg
from mlo import report
from mlo.agent import evals as evalsmod
from mlo.agent.llm import ChainClient
from test_agent_protocol import llm_cfg

EVALS = Path(__file__).resolve().parent.parent / "evals"


def client_for(world):
    return ChainClient(llm_cfg(world),
                       transport=evalsmod.heuristic_transport)


def test_classify_eval_runs_and_scores(world):
    r = evalsmod.eval_classify(
        client_for(world), str(EVALS / "classify.json"),
        ("Video", "Audio", "Photos", "Documents", "Backups"))
    assert r["items"] >= 40
    assert r["decided"] > 0
    assert r["accuracy_on_decided"] is not None
    # the heuristic labels strictly by extension, so it must ace the
    # extension-determined golds
    assert r["accuracy_on_decided"] >= 0.9


def test_triage_eval_counts_dangerous_errors_asymmetrically(world):
    media = {".jpg", ".mp4", ".mp3", ".vob", ".amr"}
    r = evalsmod.eval_triage(client_for(world), str(EVALS / "triage.json"), media)
    assert r["clusters"] >= 12
    assert r["dangerous_errors"] == 0        # guard + heuristic: no junked media
    assert r["decided"] + r["needs_human"] == r["clusters"]


def test_critics_eval_runs_and_scores(world):
    """P21/B7: the critic-panel eval runner — there was none before this."""
    cfg = llm_cfg(world)
    r = evalsmod.eval_critics(
        ChainClient(cfg, transport=evalsmod.heuristic_transport),
        cfg, str(EVALS / "critics.json"))
    assert r["task"] == "critics"
    assert r["items"] >= 15
    assert r["decided"] > 0
    assert r["accuracy_on_decided"] is not None
    assert r["abstained"] == 0            # the heuristic always answers


def test_heuristic_critic_movie_flags_personal_by_path_pattern():
    view = {"path": "Video/eSrc/whatsapp/VID-20200101-WA0001.mp4",
           "candidate_homes": ["Video/Personal"]}
    ans = evalsmod._heuristic_critic_movie(json.dumps(view))
    assert ans["media_kind"] == "personal"
    assert ans["proposed_home"] == "Video/Personal"


def test_heuristic_critic_movie_defaults_to_movie():
    view = {"path": "Video/eSrc/films/Inception.2010.mkv",
           "candidate_homes": ["Video/Movies/English"]}
    ans = evalsmod._heuristic_critic_movie(json.dumps(view))
    assert ans["media_kind"] == "movie"


def test_heuristic_critic_movie_language_is_never_bare_none():
    """The schema rejects a bare None for 'language' (require_choice has no
    None case) — the heuristic must emit UNSURE, not None."""
    from mlo.agent.protocol import UNSURE
    view = {"path": "x.mkv", "candidate_homes": ["Video/Movies/English"]}
    ans = evalsmod._heuristic_critic_movie(json.dumps(view))
    assert ans["language"] == UNSURE


def test_heuristic_critic_music_flags_personal_voice_notes():
    view = {"path": "Audio/eSrc/voice/AUD-20160126-WA0002.amr",
           "candidate_homes": ["Audio/Spoken_Word"]}
    ans = evalsmod._heuristic_critic_music(json.dumps(view))
    assert ans["media_kind"] == "personal"


def test_heuristic_critic_photo_distinguishes_screenshot_graphic_photo():
    photo = evalsmod._heuristic_critic_photo(json.dumps(
        {"path": "Images/eSrc/camera/IMG_20180902.jpg", "candidate_homes": ["Images/Photos"]}))
    screenshot = evalsmod._heuristic_critic_photo(json.dumps(
        {"path": "Images/eSrc/ui/screenshot-2024.png", "candidate_homes": ["Images/Photos"]}))
    graphic = evalsmod._heuristic_critic_photo(json.dumps(
        {"path": "Images/eSrc/icons/app-icon.png", "candidate_homes": ["Images/Photos"]}))
    assert photo["kind"] == "photo"
    assert screenshot["kind"] == "screenshot"
    assert graphic["kind"] == "graphic"


def test_heuristic_transport_dispatches_critic_specs_by_system_prompt(world):
    """heuristic_transport must route a critic-shaped request to its own
    heuristic, not the classify/triage fallback."""
    from mlo.agent.critics import movie_tv_critic_spec
    cfg = llm_cfg(world)
    spec = movie_tv_critic_spec("English", tuple(cfg.layout.languages))
    payload = {"messages": [
        {"role": "system", "content": spec.system},
        {"role": "user", "content": json.dumps(
            {"path": "Video/eSrc/x.mkv", "candidate_homes": ["Video/Movies/English"]})},
    ]}
    resp = evalsmod.heuristic_transport("url", payload, {}, 30)
    body = json.loads(resp["choices"][0]["message"]["content"])
    assert body["media_kind"] in ("movie", "personal")


def test_eval_config_forces_agent_on(world):
    cfg = make_cfg(world)
    assert not cfg.llm.enabled
    e = evalsmod.eval_config(cfg)
    assert e.llm.enabled and e.llm.local.enabled and e.llm.chain


def test_eval_config_chain_override_gates_local_slot(world):
    """A cloud-only chain must NOT wake the local slot; a chain containing
    'local' must enable it — so `--chain claude-...` measures cloud cleanly."""
    cfg = make_cfg(world)
    cloud = evalsmod.eval_config(cfg, chain=("claude-haiku-4-5",))
    assert cloud.llm.enabled and cloud.llm.chain == ("claude-haiku-4-5",)
    assert not cloud.llm.local.enabled
    escalate = evalsmod.eval_config(cfg, chain=("local", "claude-haiku-4-5"))
    assert escalate.llm.local.enabled
    assert escalate.llm.chain == ("local", "claude-haiku-4-5")


def test_runners_emit_chain_ledger(world):
    """Both eval runners surface the per-call chain ledger so the CLI can persist
    it and roll it up — the audit trail agent-design.md §1 promises."""
    r = evalsmod.eval_classify(
        client_for(world), str(EVALS / "classify.json"),
        ("Video", "Audio", "Photos", "Documents", "Backups"))
    assert r["ledger"] and all("outcome" in e for e in r["ledger"])
    answered = [e for e in r["ledger"] if e.get("outcome") == "ok"]
    assert answered and all(e.get("entry") == "local" for e in answered)


def test_agent_ledger_persist_and_summary(world):
    """report.write_agent_ledger writes a JSONL view; summarize_ledger rolls up
    calls answered, entries used, fallback hops, and average latency."""
    ledger = [
        {"entry": "local", "outcome": "failed", "latency_s": 0.2},   # a hop
        {"entry": "claude-haiku-4-5", "outcome": "ok", "latency_s": 0.9},
        {"entry": "local", "outcome": "ok", "latency_s": 0.3},
        {"tier": "any", "attempt": "first", "outcome": "ok"},        # non-transport row
    ]
    ws = world["store"].workspace
    path = report.write_agent_ledger(ws, "run-x", ledger)
    lines = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines()]
    assert len(lines) == len(ledger)
    s = report.summarize_ledger(ledger)
    assert s["calls_answered"] == 2
    assert s["fallback_hops"] == 1
    assert s["by_entry"] == {"claude-haiku-4-5": 1, "local": 1}
    assert s["avg_latency_s"] == 0.6
