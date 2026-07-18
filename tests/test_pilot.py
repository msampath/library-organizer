"""mlo pilot (Pass 1): the whole-library analysis DAG — read-only + rehearsed,
one sealed proposal. The strongest claims are asserted, not narrated: the ops
journal is provably untouched, re-runs are content-addressed idempotent, the
proposal seal refuses tampering, and every plan row lands in exactly one
cluster."""
from __future__ import annotations

import json
import os

import pytest

from conftest import make_file
from helpers import make_cfg
from mlo import pilot as pilotmod
from mlo import report
from mlo.report import PlanIntegrityError, read_proposal

TAX = {"Video": (".mp4", ".mkv"), "Audio": (".mp3",),
       "Photos": (".jpg",), "Documents": (".pdf",)}


def n(*segs):
    return os.sep.join(segs)


def seed_world(world):
    """A messy little universe: a source with a unique movie, a library twin,
    and junk; a library with a curated tree, a misplaced movie, and an
    unroutable dashcam clip."""
    lib, src = world["lib"], world["E"]
    # library
    make_file(lib / "Audio/Music/Hindi/Album/track.mp3", b"TWIN-BYTES" * 40)
    make_file(lib / "Video/eSrc/films/Heat.(1995).mkv", b"HEAT" * 100)
    make_file(lib / "Video/eSrc/dash/FILE001.mp4", b"DASH" * 50)
    # source: one unique movie, one library twin, one junk name
    make_file(src / "films/Sivaji.The.Boss.(2007).mkv", b"SIVAJI" * 100)
    make_file(src / "music/track.mp3", b"TWIN-BYTES" * 40)
    make_file(src / "Thumbs.db", b"x")


def run_analyze(world, **kw):
    cfg = make_cfg(world, taxonomy=TAX)
    st = world["store"]
    run = st.start_run("pilot-test", [], cfg.config_hash, "t")
    res = pilotmod.analyze(st, cfg, run, drive_of=world["drive_of"], **kw)
    return cfg, st, res


def by_id(res):
    return {s.id: s for s in res.sections}


def test_analyze_end_to_end_and_journal_untouched(world):
    seed_world(world)
    st = world["store"]
    pos_before = st.journal_pos()

    cfg, st, res = run_analyze(world)
    secs = by_id(res)

    # Pass 1 is provably read-only+rehearsed: the ops journal never moved
    assert st.journal_pos() == pos_before
    # ...and nothing on disk moved
    assert (world["E"] / "films/Sivaji.The.Boss.(2007).mkv").exists()
    assert (world["lib"] / "Video/eSrc/films/Heat.(1995).mkv").exists()

    # sections: organize ready (the unique movie routes), dedup gated on it
    org = secs["organize:e"]
    assert org.status == "ready" and org.n_rows >= 1
    assert org.rehearsal["would_do"] == org.n_rows       # rehearsed clean
    ded = secs["dedup:e"]
    assert ded.status == "gated" and ded.depends_on == ["organize:e"]

    # library sections: the misplaced movie plans a move; the dashcam clip is
    # unroutable and joins the review queue
    reorg = secs["reorganize:library"]
    assert reorg.status == "ready" and reorg.n_rows >= 1
    assert any("Heat" in s["dst"] for c in reorg.clusters for s in c["sample"])
    assert n("Video", "eSrc", "dash", "FILE001.mp4") \
        in res.review["unsure_relpaths"]                 # llm disabled -> human

    # execution order: organize before dedup, library sections after
    order = read_proposal(res.proposal_path)["execution_order"]
    assert order.index("organize:e") < order.index("dedup:e")
    assert order.index("dedup:e") < order.index("reorganize:library")

    # the proposal seal round-trips
    doc = read_proposal(res.proposal_path)
    assert doc["proposal_sha256"]
    assert doc["llm"]["enabled"] is False


def test_analyze_is_idempotent_content_addressed(world):
    seed_world(world)
    _, _, first = run_analyze(world)
    _, _, second = run_analyze(world)
    ids1 = {s.id: s.plan_id for s in first.sections if s.plan_id}
    ids2 = {s.id: s.plan_id for s in second.sections if s.plan_id}
    assert ids1 == ids2                       # unchanged world -> same plans


def test_coverage_blocked_source_is_a_section_not_a_crash(world):
    src = world["E"]
    for i in range(30):
        make_file(src / f"weird/file{i}.xyzext", b"?" * (i + 1))
    make_file(src / "ok/song.mp3", b"S" * 128)
    cfg = make_cfg(world, taxonomy=TAX, max_unmatched_pct=5.0)
    st = world["store"]
    run = st.start_run("pilot-test", [], cfg.config_hash, "t")
    res = pilotmod.analyze(st, cfg, run, drive_of=world["drive_of"])
    org = by_id(res)["organize:e"]
    assert org.status == "blocked"
    assert "coverage gate" in org.blocked_reason
    assert res.exit_code == 0                 # blocked is content, not failure


def test_critic_hints_flow_into_the_replan(world, monkeypatch):
    """With [llm] enabled and a scripted critic, the dashcam clip gets a
    'personal' hint and the REBUILT reorganize section routes it — the A6->A7
    seam works end to end."""
    import dataclasses

    import mlo.agent.llm as llmmod
    from test_agent_protocol import scripted

    seed_world(world)
    reply = {"media_kind": "personal", "language": "English", "year": None,
             "title": None, "proposed_home": "Video/Personal",
             "confidence": 0.9, "rationale": "dashcam numbering"}
    real = llmmod.ChainClient

    def scripted_client(cfg, transport=None):
        return real(cfg, transport=scripted([json.dumps(reply)]))

    monkeypatch.setattr(llmmod, "ChainClient", scripted_client)

    cfg = make_cfg(world, taxonomy=TAX)
    cfg = dataclasses.replace(cfg, llm=dataclasses.replace(
        cfg.llm, enabled=True, chain=("local",),
        local=dataclasses.replace(cfg.llm.local, enabled=True)))
    st = world["store"]
    run = st.start_run("pilot-test", [], cfg.config_hash, "t")
    res = pilotmod.analyze(st, cfg, run, drive_of=world["drive_of"])

    reorg = by_id(res)["reorganize:library"]
    dsts = [s["dst"] for c in reorg.clusters for s in c["sample"]]
    assert any(n("Video", "Personal", "dash", "FILE001.mp4") in d
               for d in dsts)
    doc = read_proposal(res.proposal_path)
    assert doc["llm"]["hinted"] == 1
    # the merged hints are persisted for Pass-2 convergence re-plans
    hints_path = doc["review"]["hints_path"]
    saved = json.load(open(hints_path, encoding="utf-8"))
    assert saved["Video/eSrc/dash/FILE001.mp4"]["media_kind"] == "personal"


def _scripted_critic_client(world, cfg):
    """Shared fixture-builder: an llm-enabled cfg wired to a scripted
    ChainClient returning one 'personal' verdict, for tests that need a
    critic round-trip without a real model."""
    import dataclasses

    import mlo.agent.llm as llmmod
    from test_agent_protocol import scripted

    reply = {"media_kind": "personal", "language": "English", "year": None,
             "title": None, "proposed_home": "Video/Personal",
             "confidence": 0.9, "rationale": "dashcam numbering"}
    real = llmmod.ChainClient

    def scripted_client(cfg, transport=None):
        return real(cfg, transport=scripted([json.dumps(reply)]))

    return dataclasses.replace(cfg, llm=dataclasses.replace(
        cfg.llm, enabled=True, chain=("local",),
        local=dataclasses.replace(cfg.llm.local, enabled=True))), scripted_client


def test_analyze_live_search_passes_real_search_fn_to_evidence(world, monkeypatch):
    """P21/B2: with --live-search and a configured [enrich].searxng_url,
    evidence.assemble receives a REAL search_fn — closing the 'ghost query'
    (a query composed and attached but the internet never actually queried)."""
    import dataclasses

    import mlo.agent.llm as llmmod
    import mlo.enrich.evidence as evidmod

    seed_world(world)
    cfg = make_cfg(world, taxonomy=TAX)
    cfg, scripted_client = _scripted_critic_client(world, cfg)
    cfg = dataclasses.replace(cfg, enrich=dataclasses.replace(
        cfg.enrich, searxng_url="http://localhost:8080"))
    monkeypatch.setattr(llmmod, "ChainClient", scripted_client)

    captured = {}
    real_assemble = evidmod.assemble

    def spying_assemble(items, cfg, *, search_fn=None, cache=None):
        captured["search_fn"] = search_fn
        return real_assemble(items, cfg, search_fn=search_fn, cache=cache)
    monkeypatch.setattr(evidmod, "assemble", spying_assemble)

    st = world["store"]
    run = st.start_run("pilot-test", [], cfg.config_hash, "t")
    pilotmod.analyze(st, cfg, run, drive_of=world["drive_of"], live_search=True)

    assert "search_fn" in captured
    assert captured["search_fn"] is not None


def test_analyze_without_live_search_flag_search_fn_stays_none(world, monkeypatch):
    """Default behavior (no --live-search) is unchanged: evidence.assemble
    still gets search_fn=None — queries composed, never searched."""
    import dataclasses

    import mlo.agent.llm as llmmod
    import mlo.enrich.evidence as evidmod

    seed_world(world)
    cfg = make_cfg(world, taxonomy=TAX)
    cfg, scripted_client = _scripted_critic_client(world, cfg)
    cfg = dataclasses.replace(cfg, enrich=dataclasses.replace(
        cfg.enrich, searxng_url="http://localhost:8080"))
    monkeypatch.setattr(llmmod, "ChainClient", scripted_client)

    captured = {}
    real_assemble = evidmod.assemble

    def spying_assemble(items, cfg, *, search_fn=None, cache=None):
        captured["search_fn"] = search_fn
        return real_assemble(items, cfg, search_fn=search_fn, cache=cache)
    monkeypatch.setattr(evidmod, "assemble", spying_assemble)

    st = world["store"]
    run = st.start_run("pilot-test", [], cfg.config_hash, "t")
    pilotmod.analyze(st, cfg, run, drive_of=world["drive_of"], live_search=False)

    assert captured["search_fn"] is None


def test_analyze_live_search_without_searxng_url_stays_offline(world, monkeypatch):
    """--live-search with NO [enrich].searxng_url configured must not crash —
    it silently stays on the offline path (nothing to search against)."""
    import mlo.agent.llm as llmmod
    import mlo.enrich.evidence as evidmod

    seed_world(world)
    cfg = make_cfg(world, taxonomy=TAX)
    cfg, scripted_client = _scripted_critic_client(world, cfg)
    monkeypatch.setattr(llmmod, "ChainClient", scripted_client)

    captured = {}
    real_assemble = evidmod.assemble

    def spying_assemble(items, cfg, *, search_fn=None, cache=None):
        captured["search_fn"] = search_fn
        return real_assemble(items, cfg, search_fn=search_fn, cache=cache)
    monkeypatch.setattr(evidmod, "assemble", spying_assemble)

    st = world["store"]
    run = st.start_run("pilot-test", [], cfg.config_hash, "t")
    pilotmod.analyze(st, cfg, run, drive_of=world["drive_of"], live_search=True)

    assert captured["search_fn"] is None


def test_critic_cap_overflow_is_counted_never_dropped(world):
    seed_world(world)
    import dataclasses
    cfg = make_cfg(world, taxonomy=TAX)
    cfg = dataclasses.replace(cfg, llm=dataclasses.replace(
        cfg.llm, enabled=True, chain=("local",)))
    st = world["store"]
    run = st.start_run("pilot-test", [], cfg.config_hash, "t")
    res = pilotmod.analyze(st, cfg, run, drive_of=world["drive_of"],
                           critic_limit=0)
    doc = read_proposal(res.proposal_path)
    assert doc["llm"]["capped"] == 1
    assert n("Video", "eSrc", "dash", "FILE001.mp4") \
        in doc["review"]["unsure_relpaths"]


def test_cluster_rows_partitions_every_row_exactly_once(world):
    cfg = make_cfg(world, taxonomy=TAX)
    rows = []
    for i in range(7):
        rows.append({"op_id": f"op{i}", "kind": "move_within",
                     "src": os.path.join(cfg.library_root, "A", f"f{i}.mkv"),
                     "dst": os.path.join(cfg.library_root, "Video", "Movies",
                                         "Tamil" if i % 2 else "Hindi",
                                         f"f{i}.mkv"),
                     "pre": {"size": 10 * i, "quick_hash": "q"},
                     "reason": {"verdict": "REORGANIZE", "rule": "route:movie"}})
    c1 = pilotmod.cluster_rows("reorganize", rows, cfg)
    c2 = pilotmod.cluster_rows("reorganize", rows, cfg)
    assert [c["id"] for c in c1] == [c["id"] for c in c2]          # stable
    assert [c["op_ids_sha256"] for c in c1] == [c["op_ids_sha256"] for c in c2]
    assert sum(c["n_rows"] for c in c1) == len(rows)               # partition
    assert len({s["op_id"] for c in c1 for s in c["sample"]}) <= len(rows)


def test_proposal_seal_refuses_tampering(world, tmp_path):
    st = world["store"]
    path = report.write_proposal(str(tmp_path), "run-x",
                                 {"sections": [], "execution_order": []})
    doc = read_proposal(path)                  # clean round-trip
    assert doc["schema"] == "mlo.proposal/1"

    raw = open(path, encoding="utf-8").read()
    open(path, "w", encoding="utf-8").write(
        raw.replace('"execution_order": []', '"execution_order": ["x"]'))
    with pytest.raises(PlanIntegrityError, match="hash mismatch"):
        read_proposal(path)


def test_flatten_provenance_section_appears_in_proposal(world):
    """C27: pilot builds flatten-provenance after the other library movers and
    it lands in the sealed proposal's execution_order between date-drain and
    prune-empty (so Pass-2 prune sees the emptied provenance dirs)."""
    lib = world["lib"]
    make_file(lib / "Documents/E_NAS1/taxes.pdf", b"PROVENANCE" * 10)
    make_file(lib / "Documents/E_HDD2_Part1/note.txt", b"OK" * 20)
    cfg, st, res = run_analyze(world)
    secs = by_id(res)

    fl = secs["flatten-provenance:library"]
    assert fl.status == "ready" and fl.n_rows == 2
    order = read_proposal(res.proposal_path)["execution_order"]
    assert order.index("date-drain:library") \
        < order.index("flatten-provenance:library")
    assert order.index("flatten-provenance:library") \
        < order.index("prune-empty:library")


def test_containers_section_first_in_library_order(world):
    """C33: the containers section exists, precedes every other library
    section in execution order (a unit claims its subtree before any per-file
    mover looks), and claims the whole subtree as one cluster."""
    lib = world["lib"]
    make_file(lib / "Documents/user1/Phone Backups/S5/Contacts_005.vcf",
              b"VCF" * 20)
    make_file(lib / "Documents/user1/Phone Backups/S5/notes.txt", b"N" * 20)
    cfg, st, res = run_analyze(world)
    secs = by_id(res)

    cont = secs["containers:library"]
    assert cont.status == "ready" and cont.n_rows == 2
    dsts = [s["dst"] for c in cont.clusters for s in c["sample"]]
    # D10: device-keyed, not container-name-keyed
    assert all(n("Backups", "Phones", "S5") in d for d in dsts)
    assert not any("user1" in d for d in dsts)      # no clash → no owner
    assert not any("Phone Backups" in d for d in dsts)  # container name dropped

    order = read_proposal(res.proposal_path)["execution_order"]
    for later in ("dedup-library:library", "reorganize:library",
                  "date-drain:library", "flatten-provenance:library",
                  "prune-empty:library"):
        assert order.index("containers:library") < order.index(later)


def test_pilot_flatten_leaves_media_bucket_files_alone(world):
    """C28: flatten must never touch Audio/Video/Videos/Photos/Images tops —
    the media taxonomy owns those files. Whether reorganize handles them (a
    proper devotional route), C19-blocks them (shelf), or unrouted-queues
    them (media without a derivable route), flatten stays away. Provenance
    folders inside media buckets stay as a signal that audio-triage needs
    another pattern, not silently laundered flat."""
    lib = world["lib"]
    # An audio file under a provenance folder — could route, could not; the
    # invariant is that flatten does not touch it regardless.
    make_file(lib / "Audio/G_Phone2/random_song.mp3", b"AUDIO" * 30)
    make_file(lib / "Photos/E_NAS1/some.jpg", b"IMG" * 30)
    make_file(lib / "Documents/E_NAS1/taxes.pdf", b"DOC" * 30)
    cfg, st, res = run_analyze(world)

    fl = by_id(res)["flatten-provenance:library"]
    fl_srcs = [s["src"] for c in fl.clusters for s in c["sample"]]
    # Only the Documents provenance folder is flatten's business
    assert any("Documents" in s and "E_NAS1" in s for s in fl_srcs)
    assert not any(("Audio" + os.sep) in s for s in fl_srcs)
    assert not any(("Photos" + os.sep) in s for s in fl_srcs)
