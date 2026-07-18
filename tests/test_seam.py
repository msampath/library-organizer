"""The engine->agents seam (§3.3): the self-contained review-set artifact and
the hints->gate loop — a hint reaches disk ONLY through an approved plan."""
from __future__ import annotations

import json
import os

from conftest import make_file
from helpers import make_cfg
from mlo import fingerprint, plan as planmod, provenance, seam
from mlo.apply import apply_plan
from mlo.report import write_review_set
from mlo.taxonomy import Hints

TAX = {"Video": (".mp4", ".mkv"), "Audio": (".mp3",),
       "Photos": (".jpg",), "Documents": (".pdf",)}


def n(*segs):
    return os.sep.join(segs)


def _seed(world, rels):
    st = world["store"]
    for rel in rels:
        p = make_file(world["lib"] / rel, rel.encode() * 3)
        size, qh = fingerprint.quick(str(p))
        st.index_upsert(rel.replace("/", os.sep), size, qh,
                        os.stat(p).st_mtime_ns, "seed")
    st.index_commit()
    run = st.start_run("seed", [], "cfg-test", "t")
    st.artifact_register("index:library", "index",
                         {"root": str(world["lib"])}, "cfg-test", run)


# ── A1: the review-set is self-contained and enumerates candidate homes ──────

def test_review_set_is_self_contained_with_candidate_homes(world):
    cfg = make_cfg(world, taxonomy=TAX)
    rows = [{"relpath": n("Video", "eSrc", "tamil", "Roja.mkv"),
             "size": 100, "quick_hash": "qh"}]
    it = seam.build_review_set(cfg, rows)[0]
    assert it["bucket"] == "Video"
    assert it["language_guess"] == "Tamil"       # detected from the path token
    assert it["ext"] == ".mkv" and it["size"] == 100
    homes = it["candidate_homes"]               # ENUMERATED from config, not free text
    for expect in ("Video/Movies/Tamil", "Video/TV_Shows/Tamil",
                   "Video/Personal", "Video/Unclassified"):
        assert expect in homes


def test_review_set_carries_provenance_origin(world):
    cfg = make_cfg(world, taxonomy=TAX)
    st = world["store"]
    rel = n("Video", "Personal", "clip.mp4")
    src = os.path.join(str(world["E"]), "WhatsApp", "Media", "clip.mp4")
    run = st.start_run("o", [], cfg.config_hash, "t")
    st.journal_intent(run, "p", "op", "copy_in", src,
                      os.path.join(str(world["lib"]), rel), 1, "h")
    st.complete_op("op", "done", index_effect=("insert", rel, 1, "h", 0, "s"))

    items = seam.build_review_set(
        cfg, [{"relpath": rel, "size": 1, "quick_hash": "h"}],
        origin_map=provenance.build_origin_map(st))
    assert items[0]["origin"] == src
    assert items[0]["origin_signal"] == "personal"      # INFORMS the critic


def test_review_set_from_source_root_sets_absolute_origin(world):
    """A source REVIEW pile: origin is the absolute source path (root/relpath),
    and an extension with no bucket is bucketless — the caller then content-
    sniffs it (the `agent critics --source` path)."""
    cfg = make_cfg(world, taxonomy=TAX)
    rows = [{"relpath": n("Part 2", "Others", "carve.HTM"),
             "size": 27_000_000, "quick_hash": "h"}]
    items = seam.build_review_set(cfg, rows, root=str(world["E"]))
    assert items[0]["origin"] == os.path.join(
        str(world["E"]), "Part 2", "Others", "carve.HTM")
    assert items[0]["bucket"] is None            # .HTM: no bucket -> sniff decides


def test_review_set_writes_jsonl_with_header(world):
    cfg = make_cfg(world, taxonomy=TAX)
    st = world["store"]
    run = st.start_run("r", [], cfg.config_hash, "t")
    items = seam.build_review_set(
        cfg, [{"relpath": "a.mkv", "size": 1, "quick_hash": "h"}])
    path = write_review_set(st.workspace, run, items)
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    head = json.loads(lines[0])
    assert head["schema"] == "mlo.review-set/1" and head["count"] == 1
    assert json.loads(lines[1])["relpath"] == "a.mkv"


# ── A2: hints INFORM via the gate; nothing reaches disk except via a plan ────

def test_hint_informs_via_the_gate_never_on_rehearse(world):
    """A critic hint routes an otherwise-unroutable file, but a rehearsal
    (no --execute) mutates NOTHING — only the approved execute performs it."""
    cfg = make_cfg(world, taxonomy=TAX)
    st = world["store"]
    rel = "Video/eSrc/Roja.mkv"                  # no year -> not routable alone
    _seed(world, [rel])
    hint = {n("Video", "eSrc", "Roja.mkv"):
            Hints(media_kind="movie", language="Tamil", year=1992)}
    res = planmod.build_reorganize(st, cfg, under=["Video"], hints=hint,
                                   drive_of=world["drive_of"])
    assert res.n_rows == 1
    home = world["lib"] / "Video" / "Movies" / "Tamil" / "Roja (1992)" / "Roja (1992).mkv"

    # rehearse: the file stays at its origin, the home does not exist
    apply_plan(st, cfg, res.path, st.start_run("a", [], cfg.config_hash, "t"),
               execute=False, drive_of=world["drive_of"])
    assert (world["lib"] / "Video" / "eSrc" / "Roja.mkv").exists()
    assert not home.exists()

    # execute the approved plan: only now does the hint reach disk
    r = apply_plan(st, cfg, res.path, st.start_run("b", [], cfg.config_hash, "t"),
                   execute=True, drive_of=world["drive_of"])
    assert r.exit_code == 0 and home.exists()


def test_unroutable_without_a_hint_stays_put(world):
    """No hint -> no route -> the file is reported (review-set), never guessed
    into a home: the file yields zero plan rows."""
    cfg = make_cfg(world, taxonomy=TAX)
    st = world["store"]
    _seed(world, ["Video/eSrc/mystery-clip.mkv"])
    res = planmod.build_reorganize(st, cfg, under=["Video"],
                                   drive_of=world["drive_of"])
    assert res.n_rows == 0
    assert "Video/eSrc/mystery-clip.mkv".replace("/", os.sep) in res.unrouted


def test_review_set_carries_all_signals(world):
    """CANONICAL (owner, 2026-07-09): a critic judges with ALL the signals a
    human would read — the review item carries embedded doc properties, the
    folder's siblings, and the file date, never just a filename."""
    cfg = make_cfg(world, taxonomy={"Documents": (".pptx",), "Audio": (".mp3",)})
    rows = [{"relpath": os.sep.join(("Presentations", "Unsorted", "LVC.pptx")),
             "size": 9000, "quick_hash": "q1",
             "mtime_ns": 1380844800_000000000}]      # 2013-10-04 UTC
    sib = seam.build_sibling_index([
        os.sep.join(("Presentations", "Unsorted", "LVC.pptx")),
        os.sep.join(("Presentations", "Unsorted", "scitech.pptx")),
        os.sep.join(("Presentations", "Unsorted", "hist.pptx"))])
    props = {rows[0]["relpath"]: {"title": "SPORTS QUIZ- PRELIM",
                                  "creator": "Srinath"}}
    (item,) = seam.build_review_set(cfg, rows, sibling_index=sib,
                                    doc_props=props)
    assert item["doc_props"]["title"] == "SPORTS QUIZ- PRELIM"
    assert item["mtime"] == "2013-10-04"
    assert sorted(item["siblings"]) == ["hist.pptx", "scitech.pptx"]


def test_review_set_carries_media_tags_and_title_candidates(world):
    """P21/B3: real ID3 tags and a real TMDb candidate reach the review
    item — closing the gap where critic prompts asked for evidence no
    producer ever attached."""
    cfg = make_cfg(world, taxonomy={"Audio": (".mp3",), "Video": (".mkv",)})
    rows = [{"relpath": os.sep.join(("Audio", "Unsorted", "song.mp3")),
             "size": 500, "quick_hash": "q1"},
            {"relpath": os.sep.join(("Video", "Unsorted", "movie.mkv")),
             "size": 900, "quick_hash": "q2"}]
    tags = {rows[0]["relpath"]: {"artist": "Rahman", "title": "Song"}}
    cands = {rows[1]["relpath"]: {"tmdb_id": 1, "title": "Movie", "year": 2010}}
    song, movie = seam.build_review_set(
        cfg, rows, media_tags=tags, title_candidates=cands)
    assert song["media_tags"]["artist"] == "Rahman"
    assert "title_candidates" not in song
    assert movie["title_candidates"]["tmdb_id"] == 1
    assert "media_tags" not in movie


def test_review_set_without_media_tags_or_candidates_omits_keys(world):
    """No map passed -> the keys are simply absent, not None — matches
    doc_props's existing contract."""
    cfg = make_cfg(world, taxonomy={"Audio": (".mp3",)})
    rows = [{"relpath": "a.mp3", "size": 10, "quick_hash": "q"}]
    (item,) = seam.build_review_set(cfg, rows)
    assert "media_tags" not in item and "title_candidates" not in item
