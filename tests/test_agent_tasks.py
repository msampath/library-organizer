"""Task behavior: pre-digestion shapes, option-space enforcement, the
dangerous-error guard, orchestrator choice validation."""
from __future__ import annotations

import json

from helpers_plan import make_cfg, seed_source, seed_store
from mlo.agent import tasks as tasksmod
from mlo.agent.llm import ChainClient
from test_agent_protocol import llm_cfg, scripted


def seed_review_pile(world, cfg):
    files = {
        "scrape/page1.html": b"<html>" * 200,
        "scrape/page2.html": b"<html>" * 300,
        "dvd/VTS_01_1.vob": b"V" * 9000,
        "odd/blob.qqq": b"?" * 10,
    }
    pre = seed_source(world, cfg, files)
    seed_store(world, cfg, pre,
               {rel: ("REVIEW", "no-rule-matched") for rel in files})


def test_digest_review_clusters_byte_weighted(world):
    cfg = llm_cfg(world)
    seed_review_pile(world, cfg)
    clusters = tasksmod.digest_review(world["store"], cfg, "eSrc")
    assert clusters[0].ext == ".vob"            # biggest bytes first
    assert {c.ext for c in clusters} == {".vob", ".html", ".qqq"}
    html = next(c for c in clusters if c.ext == ".html")
    assert html.count == 2 and len(html.exemplars) == 2


def test_triage_guard_downgrades_confident_junking_of_media(world):
    cfg = llm_cfg(world)
    seed_review_pile(world, cfg)
    clusters = tasksmod.digest_review(world["store"], cfg, "eSrc")
    vob_id = next(c.cid for c in clusters if c.ext == ".vob")
    html_id = next(c.cid for c in clusters if c.ext == ".html")
    reply = {"clusters": [
        {"id": vob_id, "disposition": "stage-junk",
         "rationale": "looks like disc rips", "confidence": 0.8},
        {"id": html_id, "disposition": "stage-junk",
         "rationale": "scrape", "confidence": 0.8},
    ]}
    client = ChainClient(cfg, transport=scripted([json.dumps(reply)]))
    out = tasksmod.triage_review(client, world["store"], cfg, "eSrc")
    by_ext = {d["ext"]: d for d in out["decisions"]}
    assert by_ext[".vob"]["disposition"] == "needs-human"       # guarded (L4-adjacent)
    assert by_ext[".vob"]["model_disposition"] == "stage-junk"  # honesty preserved
    assert by_ext[".html"]["disposition"] == "stage-junk"       # not media: allowed
    assert by_ext[".qqq"]["disposition"] == "needs-human"       # unanswered cluster
    assert out["guarded"] == 1


def test_classify_unmatched_buckets_proposals_and_unsure(world):
    cfg = llm_cfg(world)
    seed_review_pile(world, cfg)
    reply = {"items": [
        {"i": 0, "label": "Video", "confidence": 0.9},
        {"i": 1, "label": "UNSURE", "confidence": 0.2},
        {"i": 2, "label": "Video", "confidence": 0.95},
        {"i": 3, "label": "UNSURE", "confidence": 0.1},
    ]}
    client = ChainClient(cfg, transport=scripted([json.dumps(reply)]))
    out = tasksmod.classify_unmatched(client, world["store"], cfg, "eSrc")
    assert out["total"] == 4
    assert len(out["proposals"]) == 2
    assert all(p["label"] == "Video" for p in out["proposals"])
    assert len(out["unsure"]) == 2


def test_classify_rejects_labels_outside_taxonomy(world):
    cfg = llm_cfg(world)
    seed_review_pile(world, cfg)
    bad = json.dumps({"items": [{"i": 0, "label": "Malware", "confidence": 0.9}]})
    client = ChainClient(cfg, transport=scripted([bad]))
    out = tasksmod.classify_unmatched(client, world["store"], cfg, "eSrc",
                                      batch_size=1, limit=1)
    # invented label never validates; the item lands in UNSURE, not proposals
    assert out["proposals"] == []
    assert len(out["unsure"]) == 1


def test_next_action_validates_choice(world):
    cfg = llm_cfg(world)
    summary = {"suggested_next": [{"cmd": "mlo verify library", "why": "done"}],
               "counts": {}, "exit_code": 0}
    ok = ChainClient(cfg, transport=scripted(['{"choice": 0, "why": "verify"}']))
    assert tasksmod.next_action(ok, summary)["choice"] == 0

    bad = ChainClient(cfg, transport=scripted(['{"choice": 7, "why": "??"}']))
    assert tasksmod.next_action(bad, summary)["choice"] == "stop"

    # bool is an int subclass — `true` must not select option index 1
    boolish = ChainClient(cfg, transport=scripted(['{"choice": true, "why": "x"}']))
    assert tasksmod.next_action(boolish, summary)["choice"] == "stop"


def test_triage_nonlist_clusters_becomes_unsure_not_crash(world):
    cfg = llm_cfg(world)
    seed_review_pile(world, cfg)
    # a non-list 'clusters' must fail validation into the repair loop, never
    # raise TypeError out of the task (protocol promise)
    bad = ChainClient(cfg, transport=scripted(['{"clusters": "oops"}']))
    out = tasksmod.triage_review(bad, world["store"], cfg, "eSrc")
    assert all(d["disposition"] == "needs-human" for d in out["decisions"])


# ── classify v2: media identity hints for the router ─────────────────────────

def test_classify_media_valid_batch(world):
    cfg = llm_cfg(world)
    paths = ["flat/Roja.mkv", "cam/PXL_001.jpg"]
    reply = {"items": [
        {"i": 0, "media_kind": "movie", "language": "Tamil", "year": 1992,
         "confidence": 0.9},
        {"i": 1, "media_kind": "personal", "language": "UNSURE", "year": None,
         "confidence": 0.85},
    ]}
    client = ChainClient(cfg, transport=scripted([json.dumps(reply)]))
    out = tasksmod.classify_media(client, world["store"], cfg, None, paths)
    assert out["total"] == 2 and out["unsure"] == []
    assert out["hints"]["flat/Roja.mkv"] == {
        "media_kind": "movie", "language": "Tamil", "year": 1992}
    assert out["hints"]["cam/PXL_001.jpg"]["media_kind"] == "personal"
    assert out["hints"]["cam/PXL_001.jpg"]["language"] is None   # UNSURE -> omitted


def test_classify_media_unsure_kind_is_load_bearing(world):
    cfg = llm_cfg(world)
    reply = {"items": [{"i": 0, "media_kind": "UNSURE", "language": "Tamil",
                        "year": None, "confidence": 0.95}]}
    client = ChainClient(cfg, transport=scripted([json.dumps(reply)]))
    out = tasksmod.classify_media(client, world["store"], cfg, None, ["x/a.mkv"])
    assert out["hints"] == {} and out["unsure"] == ["x/a.mkv"]


def test_classify_media_invented_language_never_validates(world):
    cfg = llm_cfg(world)
    bad = json.dumps({"items": [{"i": 0, "media_kind": "movie",
                                 "language": "Klingon", "year": 2000,
                                 "confidence": 0.99}]})
    client = ChainClient(cfg, transport=scripted([bad]))
    out = tasksmod.classify_media(client, world["store"], cfg, None, ["x/a.mkv"])
    assert out["hints"] == {} and out["unsure"] == ["x/a.mkv"]


def test_classify_media_bool_year_rejected(world):
    cfg = llm_cfg(world)
    bad = json.dumps({"items": [{"i": 0, "media_kind": "movie",
                                 "language": "Tamil", "year": True,
                                 "confidence": 0.99}]})
    client = ChainClient(cfg, transport=scripted([bad]))
    out = tasksmod.classify_media(client, world["store"], cfg, None, ["x/a.mkv"])
    assert out["hints"] == {} and out["unsure"] == ["x/a.mkv"]


def test_classify_media_name_patterns_decide_without_model(world):
    """Distilled name patterns (docs/classification-patterns.md) settle the
    definitional cases deterministically — recorder conventions become hints,
    cache conventions become junk, and the model is never consulted."""
    cfg = llm_cfg(world)
    paths = ["Videos/G_Phone/VID-20200817-WA0012.mp4",     # WhatsApp
             "Videos/Dash/20250520050008_0000001A.MP4",    # dashcam stamp
             "Videos/Web/UnityAds-579fd4ac-m31-1000.mp4",  # ad cache
             "Videos/Web/1444697903_570x320_low_quality.mp4"]
    transport = scripted(['{"items": []}'])
    client = ChainClient(cfg, transport=transport)
    out = tasksmod.classify_media(client, world["store"], cfg, None, paths)
    assert transport.calls == []                 # zero LLM calls
    assert out["pattern_hits"] == 4
    assert out["hints"][paths[0]]["media_kind"] == "personal"
    assert out["hints"][paths[1]]["media_kind"] == "personal"
    assert {j["relpath"] for j in out["junk"]} == set(paths[2:])
    assert out["unsure"] == []


def test_classify_media_model_junk_never_becomes_a_hint(world):
    cfg = llm_cfg(world)
    reply = {"items": [{"i": 0, "media_kind": "junk", "language": "UNSURE",
                        "year": None, "confidence": 0.9}]}
    client = ChainClient(cfg, transport=scripted([json.dumps(reply)]))
    out = tasksmod.classify_media(client, world["store"], cfg, None,
                                  ["Videos/x/Vendor Promo Reel.mp4"])
    assert out["hints"] == {}
    assert [j["relpath"] for j in out["junk"]] == ["Videos/x/Vendor Promo Reel.mp4"]
    assert out["unsure"] == []


def test_classify_media_config_patterns_win_over_defaults(world):
    import dataclasses
    cfg = dataclasses.replace(
        llm_cfg(world),
        name_patterns={"junk": (r"^Introducing Seagate ",)})
    transport = scripted(['{"items": []}'])
    client = ChainClient(cfg, transport=transport)
    out = tasksmod.classify_media(
        client, world["store"], cfg, None,
        ["Videos/E_Src/Introducing Seagate Backup Plus Video_3.mp4"])
    assert transport.calls == []
    assert out["junk"][0]["why"] == "pattern:config:junk"
