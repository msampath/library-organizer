"""P13 additions: C34 nested-segment flatten, C35 non-media bucket routes +
Comics series normalization, C36 sidecar handling, C37 bad-archive detection.

Each test is small and independent; the ledger cites them directly.
"""
from __future__ import annotations

import os
import struct
import zipfile

from dataclasses import replace

from conftest import make_file
from helpers import make_cfg
from mlo import fingerprint, plan as planmod
from mlo.apply import apply_plan
from mlo.config import Layout
from mlo.report import read_plan
from mlo.taxonomy import _normalize_comic_series, route

TAX = {"Video": (".mkv", ".mp4"), "Audio": (".mp3",),
       "Photos": (".jpg", ".png"), "Documents": (".pdf",),
       "Presentations": (".ppt", ".pptx"),
       "Spreadsheets": (".xls", ".xlsx"),
       "Archives": (".zip", ".rar", ".7z"),
       "Installers": (".exe", ".msi"),
       "Comics": (".cbz", ".cbr")}


def seed(world, rels, content_by_rel=None):
    st = world["store"]
    for rel in rels:
        payload = (content_by_rel or {}).get(rel, rel.encode() * 3)
        p = make_file(world["lib"] / rel, payload)
        size, qh = fingerprint.quick(str(p))
        st.index_upsert(rel.replace("/", os.sep), size, qh,
                        os.stat(p).st_mtime_ns, "seed")
    st.index_commit()
    run = st.start_run("seed", [], "cfg-test", "t")
    st.artifact_register("index:library", "index",
                         {"root": str(world["lib"])}, "cfg-test", run)


def n(*segs):
    return os.sep.join(segs)


# ── C34 nested flatten ───────────────────────────────────────────────────────

def test_flatten_strips_provenance_at_any_depth(world):
    """C34: `Other\\I_SSD1\\User2 S8 backup\\...\\.nomedia` had two
    provenance segments; the seg-1-only rule left the inner one behind."""
    cfg = make_cfg(world, taxonomy=TAX)
    # User2 S8 backup DOES contain 'backup', matching PROVENANCE_SEG. But
    # containers would claim it as phone-backup — put it inside 'Other'
    # (non-media, non-container) instead.
    seed(world, ["Other/I_SSD1/inner/User2 S8 backup deep/file.txt"])
    st = world["store"]
    res = planmod.build_flatten_provenance(st, cfg,
                                           drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 1
    lib = cfg.library_root
    # BOTH provenance segments (I_SSD1 AND 'User2 S8 backup deep')
    # stripped; 'inner' preserved.
    assert rows[0]["dst"] == os.path.join(lib, "Other", "inner", "file.txt")


# ── C35 non-media bucket routes + Comics series normalization ────────────────

def test_normalize_comic_series_strips_trailing_issue_numbers():
    assert _normalize_comic_series("Star Wars 01") == "Star Wars"
    assert _normalize_comic_series("Star Wars") == "Star Wars"
    assert _normalize_comic_series("Marvel Illustrated 01 02") \
        == "Marvel Illustrated"
    assert _normalize_comic_series("Star Wars 3D 02") == "Star Wars 3D"
    assert _normalize_comic_series("Star Wars 0") == "Star Wars"
    # parentheticals survive (publisher annotations)
    assert _normalize_comic_series("Star Wars (Marvel)") \
        == "Star Wars (Marvel)"
    # non-number tails survive
    assert _normalize_comic_series("Star Wars Flipbook") == "Star Wars Flipbook"


def test_comics_series_normalize_reroutes(world):
    """A comic file whose series folder has a trailing issue number ('Star
    Wars 01') routes to the normalized series ('Star Wars')."""
    cfg = make_cfg(world, taxonomy=TAX)
    r = route(cfg, n("Comics", "Star Wars 01", "issue.cbz"))
    assert r is not None
    assert r.dest_relpath == n("Comics", "Star Wars", "issue.cbz")
    assert r.rule == "route:comic:series-normalize"


def test_comics_already_placed_survives_when_series_is_canonical(world):
    """A comic already in a normalized series folder stays put."""
    cfg = make_cfg(world, taxonomy=TAX)
    r = route(cfg, n("Comics", "Star Wars", "issue.cbz"))
    assert r.rule == "route:comic:already-placed"


def test_presentations_route_to_documents(world):
    """C35: Presentations bucket → `Documents\\Presentations\\`. Prior
    behaviour returned None, so files sat where the predecessor dumped them."""
    cfg = make_cfg(world, taxonomy=TAX)
    r = route(cfg, n("Presentations", "Unsorted", "deck.pptx"))
    assert r is not None
    lib_dest = r.dest_relpath
    # Unsorted parent is dropped; the file lands directly under the root
    assert lib_dest == n("Documents", "Presentations", "deck.pptx")
    assert r.rule.startswith("route:bucket:")


def test_presentations_keeps_genuine_parent(world):
    """A genuine grouping folder is preserved; only shelves/provenance drop."""
    cfg = make_cfg(world, taxonomy=TAX)
    r = route(cfg, n("Presentations", "Q4 Board", "deck.pptx"))
    assert r.dest_relpath == n("Documents", "Presentations", "Q4 Board",
                               "deck.pptx")


def test_spreadsheets_route_to_documents(world):
    cfg = make_cfg(world, taxonomy=TAX)
    r = route(cfg, n("Spreadsheets", "Unsorted", "budget.xlsx"))
    assert r.dest_relpath == n("Documents", "Spreadsheets", "budget.xlsx")


def test_archives_and_installers_stay_top_level_but_route(world):
    """Archives + Installers keep their top-level roots; but they're now
    routable (were None before), so at least the Unsorted shelf drops."""
    cfg = make_cfg(world, taxonomy=TAX)
    r = route(cfg, n("Archives", "Unsorted", "pack.zip"))
    assert r.dest_relpath == n("Archives", "pack.zip")
    r2 = route(cfg, n("Installers", "Unsorted", "setup.exe"))
    assert r2.dest_relpath == n("Installers", "setup.exe")


# ── C36 sidecar handling ─────────────────────────────────────────────────────

def test_sidecar_srt_and_poster_follow_movie(world):
    """A movie's .srt and poster.jpg in the same source folder travel with
    the anchor to its destination folder."""
    cfg = make_cfg(world, taxonomy=TAX)
    # A misplaced movie in a wrong media root (post-C23 drain territory)
    seed(world, [
        "Videos/dump/Sivaji.The.Boss.(2007).mkv",
        "Videos/dump/Sivaji.The.Boss.(2007).srt",
        "Videos/dump/poster.jpg",
        "Videos/dump/movie.nfo",
    ])
    st = world["store"]
    res = planmod.build_reorganize(st, cfg, under=["Videos"],
                                   drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    dst_folder = os.sep.join([
        cfg.library_root, "Video", "Movies", "Other",
        "Sivaji The Boss (2007)"])
    dsts = {r["dst"] for r in rows}
    # anchor moves to the Jellyfin folder
    anchor_dst = os.path.join(dst_folder, "Sivaji The Boss (2007).mkv")
    assert anchor_dst in dsts
    # sidecars follow to the SAME folder (keeping their original names)
    assert os.path.join(dst_folder, "Sivaji.The.Boss.(2007).srt") in dsts
    assert os.path.join(dst_folder, "poster.jpg") in dsts
    assert os.path.join(dst_folder, "movie.nfo") in dsts
    sidecar_rows = [r for r in rows
                    if r["reason"]["rule"] == "route:sidecar:with-anchor"]
    assert len(sidecar_rows) == 3
    assert any("C36 sidecars moved with anchor: 3" in note
               for note in res.notes)


def test_sidecar_of_c21_blocked_anchor_does_not_orphan(world):
    """C38 regression: when an anchor is C21-blocked (fingerprint twin exists
    elsewhere), its sidecars must not emit as orphan rows. Live trigger: the
    user renamed extensionless `Movies\\English\\1408 (2007)` to
    `Movies\\English\\1408 (2007).mp4`; index kept both, they share a
    fingerprint, C21 removed the .mp4 anchor, and the old sidecar-order left
    an orphan sidecar row pointing at a folder no anchor would create."""
    cfg = make_cfg(world, taxonomy=TAX)
    same = b"MOVIE-BYTES" * 500
    seed(world, [
        "Videos/dump/Sivaji.(2007).mkv",           # anchor A
        "Videos/dump/Sivaji.(2007).srt",           # sidecar of A
        "Videos/other/Sivaji.(2007).mkv",          # twin of A -> both C21-blocked
    ], content_by_rel={
        "Videos/dump/Sivaji.(2007).mkv": same,
        "Videos/other/Sivaji.(2007).mkv": same,
    })
    st = world["store"]
    res = planmod.build_reorganize(st, cfg, under=["Videos"],
                                   drive_of=world["drive_of"])
    if res.n_rows:
        _, rows, _ = read_plan(res.path)
    else:
        rows = []
    srcs = {r["src"] for r in rows}
    # C21 blocks both .mkv anchors; the .srt sidecar has no surviving anchor
    # → NO rows emitted at all (no orphans, no anchors).
    assert not any(".mkv" in s for s in srcs)
    assert not any(".srt" in s for s in srcs)


def test_sidecar_of_unmoved_anchor_stays_put(world):
    """A poster.jpg in a folder whose media file is NOT moving stays put —
    no anchor means no sidecar."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed(world, [
        # Already-placed movie: no move
        "Video/Movies/Other/Foo (2020)/Foo (2020).mkv",
        "Video/Movies/Other/Foo (2020)/poster.jpg",
    ])
    st = world["store"]
    res = planmod.build_reorganize(st, cfg, drive_of=world["drive_of"])
    if res.n_rows:
        _, rows, _ = read_plan(res.path)
        srcs = {r["src"] for r in rows}
        assert not any("poster.jpg" in s for s in srcs)


# ── C37 bad-archive detection ────────────────────────────────────────────────

def _zip_bytes(files: dict[str, bytes]) -> bytes:
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_bad_archives_flags_broken_zip_and_bad_magic(world):
    cfg = make_cfg(world, taxonomy=TAX)
    # good zip
    good_bytes = _zip_bytes({"a.txt": b"hi"})
    make_file(world["lib"] / "Archives/good.zip", good_bytes)
    # broken zip — truncate central directory
    make_file(world["lib"] / "Archives/broken.zip", good_bytes[:20])
    # bad magic — a rar with a wrong header
    make_file(world["lib"] / "Archives/fake.rar", b"NOT-AN-ARCHIVE")
    # empty file
    make_file(world["lib"] / "Archives/empty.zip", b"")
    # not an archive at all — different ext, ignored
    make_file(world["lib"] / "Archives/notes.txt", b"skip me")
    st = world["store"]
    for rel in ("Archives/good.zip", "Archives/broken.zip",
                "Archives/fake.rar", "Archives/empty.zip"):
        p = str(world["lib"] / rel)
        size, qh = fingerprint.quick(p)
        st.index_upsert(rel.replace("/", os.sep), size, qh,
                        os.stat(p).st_mtime_ns, "seed")
    st.index_commit()
    run = st.start_run("seed", [], "cfg-test", "t")
    st.artifact_register("index:library", "index",
                         {"root": str(world["lib"])}, "cfg-test", run)

    res = planmod.build_bad_archives(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    srcs = {os.path.basename(r["src"]) for r in rows}
    assert "broken.zip" in srcs
    assert "fake.rar" in srcs
    assert "empty.zip" in srcs
    assert "good.zip" not in srcs
    # all bad ones stage to the configured staging root (drive letter maps)
    assert all(r["kind"] == "stage_move" for r in rows)
    assert all(r["reason"]["verdict"] == "BAD_ARCHIVE" for r in rows)


def test_sidecar_of_collision_skipped_anchor_does_not_orphan(world):
    """C40 regression: an anchor dropped by the DEST-COLLISION path (occupied
    by a different-content file) must not shed its sidecars into the occupied
    folder — same orphan class as C38, reached through L17 instead of C21."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed(world, [
        "Videos/dump/Sivaji.The.Boss.(2007).mkv",     # anchor, unique content
        "Videos/dump/Sivaji.The.Boss.(2007).srt",     # its sidecar
        # different-content occupant at the anchor's routed destination
        "Video/Movies/Other/Sivaji The Boss (2007)/Sivaji The Boss (2007).mkv",
    ])
    st = world["store"]
    res = planmod.build_reorganize(st, cfg, under=["Videos"],
                                   drive_of=world["drive_of"])
    rows = read_plan(res.path)[1] if res.n_rows else []
    srcs = {r["src"] for r in rows}
    # anchor collides (stays), so the .srt must stay too — zero rows
    assert not any(".srt" in s for s in srcs)
    assert not any("dump" in s and ".mkv" in s for s in srcs)
    assert any("stayed with unmoved anchor (C40): 1" in note
               for note in res.notes)


def test_sidecar_same_stem_same_bucket_is_not_a_sidecar(world):
    """C41 regression: a same-stem sibling in the anchor's OWN bucket
    (Sivaji.mp4 beside the Sivaji.mkv anchor) is an alternate copy — a
    dedup/placement decision — and must never ride along as a sidecar.
    Here the .mp4 is C21-blocked (fingerprint twin elsewhere); the old
    same-stem rule moved it anyway under the sidecar exemption."""
    cfg = make_cfg(world, taxonomy=TAX)
    same = b"TWIN-MP4-BYTES" * 500
    seed(world, [
        "Videos/dump/Sivaji.The.Boss.(2007).mkv",     # unique anchor: moves
        "Videos/dump/Sivaji.The.Boss.(2007).mp4",     # same-stem twin copy
        "Videos/dump/Sivaji.The.Boss.(2007).srt",     # genuine sidecar
        "Videos/other/twin.mp4",                      # the .mp4's fp twin
    ], content_by_rel={
        "Videos/dump/Sivaji.The.Boss.(2007).mp4": same,
        "Videos/other/twin.mp4": same,
    })
    st = world["store"]
    res = planmod.build_reorganize(st, cfg, under=["Videos"],
                                   drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    moved = {os.path.basename(r["src"]): r["reason"]["rule"] for r in rows}
    assert "Sivaji.The.Boss.(2007).mkv" in moved          # anchor moves
    assert "Sivaji.The.Boss.(2007).srt" in moved          # sidecar follows
    assert moved["Sivaji.The.Boss.(2007).srt"] == "route:sidecar:with-anchor"
    assert "Sivaji.The.Boss.(2007).mp4" not in moved      # twin copy STAYS


def test_sidecar_cross_bucket_same_stem_still_follows(world):
    """C41 boundary: same-stem cover art (.jpg, Photos bucket) beside a
    Video anchor IS still a sidecar — only same-bucket siblings are copies."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed(world, [
        "Videos/dump/Sivaji.The.Boss.(2007).mkv",
        "Videos/dump/Sivaji.The.Boss.(2007).jpg",     # cover art, same stem
    ])
    st = world["store"]
    res = planmod.build_reorganize(st, cfg, under=["Videos"],
                                   drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    sidecars = [r for r in rows
                if r["reason"]["rule"] == "route:sidecar:with-anchor"]
    assert any(r["src"].endswith(".jpg") for r in sidecars)


def test_lrc_follows_music_anchor(world):
    """A .lrc lyric file (same stem as its .mp3, no taxonomy bucket of its
    own — a different, non-media companion, unlike an .mp4 alternate copy)
    travels with its song to the routed destination folder."""
    tax = {**TAX, "Audio": (".mp3",)}
    cfg = make_cfg(world, taxonomy=tax)
    seed(world, [
        "Audio/dump/Song.mp3",
        "Audio/dump/Song.lrc",
    ])
    st = world["store"]
    res = planmod.build_reorganize(st, cfg, under=["Audio"],
                                   drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    moved = {os.path.basename(r["src"]): r["reason"]["rule"] for r in rows}
    assert "Song.mp3" in moved
    assert moved.get("Song.lrc") == "route:sidecar:with-anchor"
    mp3_dst = next(r["dst"] for r in rows if r["src"].endswith("Song.mp3"))
    lrc_dst = next(r["dst"] for r in rows if r["src"].endswith("Song.lrc"))
    assert os.path.dirname(mp3_dst) == os.path.dirname(lrc_dst)


def test_lrc_follows_voice_note_anchor(world):
    """The actual live-run gap: a voice-note anchor (audioclass 'voice',
    routed via route:audio:voice — a DIFFERENT prefix than route:music:*)
    used to drop its same-stem .lrc, because 'route:audio:' was missing
    from _SIDECAR_ANCHOR_RULE_PREFIXES even though 'route:music:' was
    already present."""
    tax = {**TAX, "Audio": (".mp3",)}
    cfg = make_cfg(world, taxonomy=tax)
    seed(world, [
        "Audio/dump/PTT-20200101-WA0001.mp3",
        "Audio/dump/PTT-20200101-WA0001.lrc",
    ])
    st = world["store"]
    res = planmod.build_reorganize(st, cfg, under=["Audio"],
                                   drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    moved = {os.path.basename(r["src"]): r["reason"]["rule"] for r in rows}
    assert moved.get("PTT-20200101-WA0001.mp3") == "route:audio:voice"
    assert moved.get("PTT-20200101-WA0001.lrc") == "route:sidecar:with-anchor"


def test_music_same_stem_media_pair_not_sidecars(world):
    """C41 unchanged: a Song.mp3/Song.m4a media pair (both in the Audio
    bucket) are alternate copies, not sidecars of each other — the twin
    stays put (C21-blocked) instead of riding along with the anchor."""
    tax = {**TAX, "Audio": (".mp3", ".m4a")}
    cfg = make_cfg(world, taxonomy=tax)
    same = b"TWIN-M4A-BYTES" * 500
    seed(world, [
        "Audio/dump/Song.mp3",
        "Audio/dump/Song.m4a",
        "Audio/other/twin.m4a",
    ], content_by_rel={
        "Audio/dump/Song.m4a": same,
        "Audio/other/twin.m4a": same,
    })
    st = world["store"]
    res = planmod.build_reorganize(st, cfg, under=["Audio"],
                                   drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    moved = {os.path.basename(r["src"]): r["reason"]["rule"] for r in rows}
    assert "Song.mp3" in moved
    assert "Song.m4a" not in moved
