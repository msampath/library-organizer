"""plan reorganize — the library-repair contract (v0.2 / Part C):
scoped examination, idempotence on correct trees, convergence to zero rows,
collisions stay put. This is the machinery that must never touch the
already-properly-organized parts of a real library."""
from __future__ import annotations

import os
from pathlib import Path

from conftest import make_file
from helpers import make_cfg
from mlo import fingerprint, plan as planmod
from mlo.apply import apply_plan
from mlo.taxonomy import Hints

TAX = {"Video": (".mp4", ".mkv"), "Audio": (".mp3",),
       "Photos": (".jpg",), "Documents": (".pdf",)}
TAX_RAW = {**TAX, "Photos": (".jpg", ".dng", ".kdc"),
           "Backups": (".zip", ".crypt12")}


def seed_library(world, rels: list[str]) -> None:
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


def n(*segs):
    return os.sep.join(segs)


def test_reorganize_routes_raw_pile_and_spares_backups(world):
    """The needs-human RAW pile (Part C2): a .kdc/.dng with an attested EXIF year
    routes into Images/Photos/<year> — evidence-backed, so it moves. A yearless
    RAW routes to Photos/Unsorted, which the C19 evidence rule DROPS (relocation
    to a shelf, not a home): it stays in place, never laundered. And .crypt12 in
    the same scope (Backups, non-media) stays put too. Routed, never staged."""
    cfg = make_cfg(world, taxonomy=TAX_RAW)
    raw_dated = "Other/Unsorted/DCP_0001.kdc"
    raw_bare = "Other/Unsorted/IMG_1234.dng"
    crypt = "Other/Unsorted/msgstore.crypt12"
    seed_library(world, [raw_dated, raw_bare, crypt])
    st = world["store"]

    hints = {n("Other", "Unsorted", "DCP_0001.kdc"): Hints(year=2009)}
    res = planmod.build_reorganize(
        st, cfg, under=["Other"], hints=hints, drive_of=world["drive_of"])

    from mlo.report import read_plan
    _, rows, _ = read_plan(res.path)
    dests = {r["dst"] for r in rows}
    srcs = {r["src"] for r in rows}
    # only the year-attested RAW moves — a home, not a shelf
    assert os.path.join(cfg.library_root, "Images", "Photos", "2009",
                        "DCP_0001.kdc") in dests
    assert res.n_rows == 1
    # the yearless RAW (would be Photos/Unsorted) and the WhatsApp backup
    # (non-media bucket) are never rows — both stay put
    for stays in (raw_bare, crypt):
        assert os.path.join(cfg.library_root,
                            stays.replace("/", os.sep)) not in srcs

    # execute, then reorganize converges to zero rows (idempotence)
    r = apply_plan(st, cfg, res.path,
                   st.start_run("a", [], cfg.config_hash, "t"),
                   execute=True, drive_of=world["drive_of"])
    assert r.exit_code == 0 and r.counts == {"done": 1}
    res2 = planmod.build_reorganize(
        st, cfg, under=["Other"], hints=hints, drive_of=world["drive_of"])
    assert res2.n_rows == 0


def test_reorganize_moves_flat_mess_and_spares_correct_trees(world):
    cfg = make_cfg(world, taxonomy=TAX)
    proper_movie = "Video/Movies/Tamil/Roja (1992)/Roja (1992).mkv"
    proper_music = "Audio/Music/Hindi/Album/track.mp3"
    flat_movie = "Video/eSrc/films/Sivaji.The.Boss.(2007).mkv"
    flat_photo = "Photos/eSrc/PXL_001.jpg"
    dashcam = "Video/eSrc/dash/FILE001.mp4"
    doc = "Documents/eSrc/tax.pdf"
    seed_library(world, [proper_movie, proper_music, flat_movie, flat_photo,
                         dashcam, doc])
    st = world["store"]

    hints = {n("Photos", "eSrc", "PXL_001.jpg"): Hints(year=2026)}
    res = planmod.build_reorganize(
        st, cfg, under=["Video", "Photos", "Audio", "Documents"],
        hints=hints, drive_of=world["drive_of"])

    from mlo.report import read_plan
    _, rows, _ = read_plan(res.path)
    dests = {r["dst"] for r in rows}
    # the flat mess gets meaningful, content-derived homes (clean-named)
    assert os.path.join(
        cfg.library_root, "Video", "Movies", "Other",
        "Sivaji The Boss (2007)", "Sivaji The Boss (2007).mkv") in dests
    assert os.path.join(
        cfg.library_root, "Images", "Photos", "2026", "PXL_001.jpg") in dests
    # the correct trees, the unroutable dashcam clip, and the document
    # produce NO rows at all
    srcs = {r["src"] for r in rows}
    for untouched in (proper_movie, proper_music, dashcam, doc):
        assert os.path.join(cfg.library_root,
                            untouched.replace("/", os.sep)) not in srcs
    assert res.n_rows == 2

    # execute, then a second reorganize converges to zero rows
    r = apply_plan(st, cfg, res.path, st.start_run("a", [], cfg.config_hash, "t"),
                   execute=True, drive_of=world["drive_of"])
    assert r.exit_code == 0 and r.counts == {"done": 2}
    assert (world["lib"] / "Video" / "Movies" / "Other"
            / "Sivaji The Boss (2007)" / "Sivaji The Boss (2007).mkv").exists()
    assert not (world["lib"] / "Video" / "eSrc" / "films"
                / "Sivaji.The.Boss.(2007).mkv").exists()

    res2 = planmod.build_reorganize(
        st, cfg, under=["Video", "Photos", "Audio", "Documents"],
        hints=hints, drive_of=world["drive_of"])
    assert res2.n_rows == 0                      # converged


def test_reorganize_sniffs_false_carves_into_holding_pen(world):
    """T4: a false-carve in Other/Unsorted — a '.swf' that is really FLV video,
    a '.dat' that is really AU audio — routes by content_kind into its media
    type's Unclassified pen and MOVES (evidence-backed reclassification), while
    a headerless blob (no content_kind) stays put. After executing, a second
    reorganize under Other converges to zero (the carves left the scope)."""
    cfg = make_cfg(world, taxonomy=TAX)
    carve_v = "Other/Unsorted/recovered_0012.swf"
    carve_a = "Other/Unsorted/beep.dat"
    blob = "Other/Unsorted/mystery.bin"              # no content_kind -> stays
    seed_library(world, [carve_v, carve_a, blob])
    st = world["store"]

    hints = {n("Other", "Unsorted", "recovered_0012.swf"): Hints(content_kind="video"),
             n("Other", "Unsorted", "beep.dat"): Hints(content_kind="audio")}
    res = planmod.build_reorganize(st, cfg, under=["Other"], hints=hints,
                                   drive_of=world["drive_of"])
    from mlo.report import read_plan
    _, rows, _ = read_plan(res.path)
    dests = {r["dst"] for r in rows}
    srcs = {r["src"] for r in rows}
    assert res.n_rows == 2
    assert os.path.join(cfg.library_root, "Video", "Unclassified", "Unsorted",
                        "recovered_0012.swf") in dests
    assert os.path.join(cfg.library_root, "Audio", "Unclassified", "Unsorted",
                        "beep.dat") in dests
    assert os.path.join(cfg.library_root, blob.replace("/", os.sep)) not in srcs

    r = apply_plan(st, cfg, res.path,
                   st.start_run("a", [], cfg.config_hash, "t"),
                   execute=True, drive_of=world["drive_of"])
    assert r.exit_code == 0 and r.counts == {"done": 2}
    res2 = planmod.build_reorganize(st, cfg, under=["Other"], hints=hints,
                                    drive_of=world["drive_of"])
    assert res2.n_rows == 0                           # carves left Other/ -> converged


def test_sniff_flv_carve_end_to_end(world):
    """T4 acceptance, end to end: a real FLV-headed '.swf' in Other/Unsorted is
    sniffed BY CONTENT (magic bytes), hinted 'video', and reorganizes into the
    Video holding pen — 'to Video, not Other'. A headerless blob is left put."""
    from mlo.hints import augment_sniff_library
    cfg = make_cfg(world, taxonomy=TAX)
    st = world["store"]
    seed_with_content(world, {
        "Other/Unsorted/recovered_0012.swf":
            b"FLV\x01\x05\x00\x00\x00\x09" + b"\x00" * 40,
        "Other/Unsorted/mystery.bin": b"plain text notes, nothing to sniff here",
    })
    hints = augment_sniff_library(cfg, st, ["Other"], {})
    assert hints[n("Other", "Unsorted", "recovered_0012.swf")].content_kind == "video"
    assert n("Other", "Unsorted", "mystery.bin") not in hints   # honest no-kind

    res = planmod.build_reorganize(st, cfg, under=["Other"], hints=hints,
                                   drive_of=world["drive_of"])
    from mlo.report import read_plan
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 1
    assert rows[0]["dst"] == os.path.join(
        cfg.library_root, "Video", "Unclassified", "Unsorted", "recovered_0012.swf")


def test_scoping_is_a_hard_boundary(world):
    cfg = make_cfg(world, taxonomy=TAX)
    outside = "Video/old-tree/Inception (2010) 1080p.mkv"   # routable, but out of scope
    seed_library(world, [outside])
    res = planmod.build_reorganize(world["store"], cfg, under=["Photos"],
                                   drive_of=world["drive_of"])
    assert res.n_rows == 0
    # not vacuous: the same file IS routable when in scope
    res2 = planmod.build_reorganize(world["store"], cfg, under=["Video"],
                                    drive_of=world["drive_of"])
    assert res2.n_rows == 1


def test_destination_collisions_stay_put(world):
    cfg = make_cfg(world, taxonomy=TAX)
    a = "Video/eSrc/x/Inception (2010).mkv"
    b = "Video/eSrc/y/Inception (2010).mkv"     # same content-derived dest
    seed_library(world, [a, b])
    res = planmod.build_reorganize(world["store"], cfg, under=["Video"],
                                   drive_of=world["drive_of"])
    assert res.n_rows == 0
    assert any("collisions (stay put): 2" in note for note in res.notes)


def test_occupied_destination_in_index_stays_put(world):
    cfg = make_cfg(world, taxonomy=TAX)
    placed = "Video/Movies/Other/Inception (2010)/Inception (2010).mkv"
    dup = "Video/eSrc/Inception (2010).mkv"     # routes onto the placed one
    seed_library(world, [placed, dup])
    res = planmod.build_reorganize(world["store"], cfg, under=["Video"],
                                   drive_of=world["drive_of"])
    # the placed file is idempotent; the flat dup would collide with an
    # existing index entry -> stays put for the human/dedup pass
    assert res.n_rows == 0
    assert any("already placed: 1" in note for note in res.notes)
    assert any("collisions (stay put): 1" in note for note in res.notes)


def test_no_evidence_relocations_stay_put(world):
    """C19: an UNSCOPED reorganize must not launder recovery junk into curated
    trees. A photo with no year routed 'to Unsorted', or music whose only
    language is the default shelf, is relocation without evidence -> stays put
    and is counted. Only the Tamil track (a language token = evidence) moves.
    The scoped-drain exception is C23, tested separately."""
    cfg = make_cfg(world, taxonomy=TAX)
    blind_photo = "Photos/recovered/FILE0001.jpg"    # no EXIF year
    blind_audio = "Audio/recovered/FILE0002.mp3"     # no language anywhere
    tamil_audio = "Audio/eSrc/tamil/song.mp3"        # dir segment = evidence
    seed_library(world, [blind_photo, blind_audio, tamil_audio])
    st = world["store"]

    res = planmod.build_reorganize(st, cfg, drive_of=world["drive_of"])  # unscoped
    from mlo.report import read_plan
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 1                           # only the Tamil track
    assert rows[0]["dst"] == os.path.join(
        cfg.library_root, "Audio", "Music", "Tamil", "song.mp3")
    assert any("no-evidence relocation (stay put): 2" in note
               for note in res.notes)


def test_provenance_folder_in_media_root_auto_drains_unscoped(world):
    """C31: a file directly under a provenance folder inside a media root
    (Audio/G_Phone2/song.mp3, Photos/E_NAS1/pic.jpg) auto-drains into the
    canonical Unsorted home even without --under. The provenance folder name
    IS the drain intent; the operator doesn't have to re-declare it. Under
    Other/ (non-media, the recovery pile) C19 stays in full."""
    cfg = make_cfg(world, taxonomy=TAX)
    prov_song = "Audio/I_SSD1/some song.mp3"
    prov_photo = "Photos/E_NAS1/FILE001.jpg"
    other_song = "Other/E_NAS1/random.mp3"        # non-media stays put
    seed_library(world, [prov_song, prov_photo, other_song])
    st = world["store"]

    res = planmod.build_reorganize(st, cfg, drive_of=world["drive_of"])
    from mlo.report import read_plan
    _, rows, _ = read_plan(res.path)
    dsts = {r["dst"] for r in rows}
    # media-root provenance files DO move (C31)
    assert os.path.join(cfg.library_root, "Audio", "Music", "Unsorted",
                        "some song.mp3") in dsts
    assert os.path.join(cfg.library_root, "Images", "Photos", "Unsorted",
                        "FILE001.jpg") in dsts
    # Other\ recovery pile does NOT (C19 in full)
    assert not any("Other" in r["src"] and r["src"].endswith("random.mp3")
                   for r in rows)
    assert res.n_rows == 2
    # notes highlight the auto-drain count for the operator
    assert any("C31 provenance auto-drain" in note and "2 files" in note
               for note in res.notes)


def test_scoped_drain_relocates_wrong_media_shelf(world):
    """C23: a SCOPED (--under) reorganize of a wrong MEDIA root drains its files
    into the canonical tree even to a shelf (Unsorted / default language). The
    operator's --under scope PLUS a media-typed source (Photos\\, Audio\\ — not
    the Other\\ recovery pile, which stays: see the raw-pile test) is the drain
    declaration. Loose dump songs route FLAT — no provenance folder leaks."""
    cfg = make_cfg(world, taxonomy=TAX)
    blind_photo = "Photos/recovered/FILE0001.jpg"    # no year -> Photos\Unsorted
    blind_song = "Audio/recovered/mystery song.mp3"  # no language -> Music\Unsorted (C30)
    seed_library(world, [blind_photo, blind_song])
    st = world["store"]

    res = planmod.build_reorganize(st, cfg, under=["Photos", "Audio"],
                                   drive_of=world["drive_of"])
    from mlo.report import read_plan
    _, rows, _ = read_plan(res.path)
    dsts = {r["dst"] for r in rows}
    assert os.path.join(cfg.library_root, "Images", "Photos", "Unsorted",
                        "FILE0001.jpg") in dsts
    assert os.path.join(cfg.library_root, "Audio", "Music", "Unsorted",
                        "mystery song.mp3") in dsts   # FLAT, no 'recovered' leak
    assert res.n_rows == 2


def test_duplicate_content_stays_put(world):
    """C21 (first real repair plan): a file whose fingerprint twin exists
    anywhere in the library is a DEDUP decision, not a placement decision —
    545 of the plan's 'personal' moves were cross-source consolidation twins
    that would have been blessed into the curated tree."""
    cfg = make_cfg(world, taxonomy=TAX)
    st = world["store"]
    same = b"IDENTICAL-BYTES" * 40
    for rel, content in (("Photos/dumpA/pic.jpg", same),
                         ("Photos/dumpB/copy-of-pic.jpg", same),
                         ("Photos/dumpA/uniq.jpg", b"DIFFERENT" * 40)):
        p = make_file(world["lib"] / rel, content)
        size, qh = fingerprint.quick(str(p))
        st.index_upsert(rel.replace("/", os.sep), size, qh,
                        os.stat(p).st_mtime_ns, "seed")
    st.index_commit()
    run = st.start_run("seed", [], "cfg-test", "t")
    st.artifact_register("index:library", "index",
                         {"root": str(world["lib"])}, "cfg-test", run)

    hints = {rel.replace("/", os.sep): Hints(year=2020)
             for rel in ("Photos/dumpA/pic.jpg", "Photos/dumpB/copy-of-pic.jpg",
                         "Photos/dumpA/uniq.jpg")}
    res = planmod.build_reorganize(st, cfg, under=["Photos"], hints=hints,
                                   drive_of=world["drive_of"])
    assert res.n_rows == 1                        # only the unique file moves
    from mlo.report import read_plan
    _, rows, _ = read_plan(res.path)
    assert rows[0]["src"].endswith("uniq.jpg")
    assert any("duplicate content (stay put): 2" in note
               for note in res.notes)


def seed_with_content(world, files: dict[str, bytes]) -> None:
    st = world["store"]
    for rel, content in files.items():
        p = make_file(world["lib"] / rel, content)
        size, qh = fingerprint.quick(str(p))
        st.index_upsert(rel.replace("/", os.sep), size, qh,
                        os.stat(p).st_mtime_ns, "seed")
    st.index_commit()
    run = st.start_run("seed", [], "cfg-test", "t")
    st.artifact_register("index:library", "index",
                         {"root": str(world["lib"])}, "cfg-test", run)


def test_dedup_library_stages_confirmed_twins(world):
    """C21's counterpart: fingerprint twins nominate, FULL SHA-256 confirms,
    extras stage out (curated copy is canonical), the index drops staged rows
    transactionally, and the loop converges to zero."""
    cfg = make_cfg(world, taxonomy=TAX)
    same = b"TWIN-CONTENT" * 50
    seed_with_content(world, {
        "Videos/dumpA/x.mp4": same,
        "Videos/dumpB/x_copy.mp4": same,
        "Video/Personal/G/x.mp4": same,          # curated twin: canonical
        "Videos/dumpA/unique.mp4": b"ONLY-ONE" * 50,
    })
    st = world["store"]
    res = planmod.build_dedup_library(st, cfg, under=["Videos"],
                                      drive_of=world["drive_of"])
    assert res.n_rows == 2                        # both dump copies stage
    from mlo.report import read_plan
    _, rows, _ = read_plan(res.path)
    staging = os.path.join(str(world["I"]), "Delete", "dedup")
    assert all(r["dst"].startswith(staging) for r in rows)
    assert all("Personal" in r["reason"]["rule"] for r in rows)  # dup:keep:<canonical>
    assert any("confirmed groups: 1" in n for n in res.notes)

    r = apply_plan(st, cfg, res.path, st.start_run("a", [], cfg.config_hash, "t"),
                   execute=True, drive_of=world["drive_of"])
    assert r.exit_code == 0 and r.counts == {"done": 2}
    assert (world["lib"] / "Video" / "Personal" / "G" / "x.mp4").exists()
    assert not (world["lib"] / "Videos" / "dumpA" / "x.mp4").exists()
    left = {row["relpath"] for row in st.index_iter()}
    assert os.path.join("Videos", "dumpA", "x.mp4") not in left      # index dropped
    assert os.path.join("Videos", "dumpA", "unique.mp4") in left

    res2 = planmod.build_dedup_library(st, cfg, under=["Videos"],
                                       drive_of=world["drive_of"])
    assert res2.n_rows == 0                       # converged


def test_dedup_library_never_stages_from_layout_roots(world):
    """C22 (caught reviewing the first real dedup plan before execution):
    `--under Audio` covers Audio/Music — the curated copy must be the one
    that STAYS, never a staged extra, whatever the scope says."""
    cfg = make_cfg(world, taxonomy=TAX)
    same = b"CURATED-TWIN" * 60
    seed_with_content(world, {
        "Audio/Music/Tamil/song.mp3": same,      # curated: always canonical
        "Audio/dumpX/song.mp3": same,
    })
    res = planmod.build_dedup_library(world["store"], cfg, under=["Audio"],
                                      drive_of=world["drive_of"])
    from mlo.report import read_plan
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 1
    assert rows[0]["src"].endswith(os.path.join("dumpX", "song.mp3"))
    assert "Music" in rows[0]["reason"]["rule"]   # dup:keep:<curated canonical>


def test_dedup_library_quick_fp_collision_stays_put(world):
    """Same head+tail+size but different middle: quick fingerprints nominate,
    the full-hash confirmation refuses — nothing stages, the note says so."""
    cfg = make_cfg(world, taxonomy=TAX)
    head, tail = b"H" * 131072, b"T" * 131072
    seed_with_content(world, {
        "Videos/a/f.mp4": head + b"MIDDLE-ONE" + tail,
        "Videos/b/f.mp4": head + b"MIDDLE-TWO" + tail,
    })
    res = planmod.build_dedup_library(world["store"], cfg, under=["Videos"],
                                      drive_of=world["drive_of"])
    assert res.n_rows == 0
    assert any("quick-fp collisions (stay put): 1" in n for n in res.notes)


def test_dedup_library_ignores_zero_byte_files(world):
    cfg = make_cfg(world, taxonomy=TAX)
    seed_with_content(world, {
        "Videos/a/empty1.mp4": b"",
        "Videos/b/empty2.mp4": b"",
    })
    res = planmod.build_dedup_library(world["store"], cfg, under=["Videos"],
                                      drive_of=world["drive_of"])
    assert res.n_rows == 0                        # junk territory, not dedup


def test_inode_of_fast_filter_and_unreadable(world):
    """P21/A5: st_nlink <= 1 (no other link exists) and an unreadable path
    both return None — the fast-path that keeps _inode_of cheap for the
    overwhelming common case of un-hardlinked files."""
    cfg = make_cfg(world, taxonomy=TAX)
    make_file(world["lib"] / "Videos" / "solo.mp4", b"x" * 10)
    assert planmod._inode_of(cfg, os.path.join("Videos", "solo.mp4")) is None
    assert planmod._inode_of(cfg, os.path.join("Videos", "gone.mp4")) is None


def test_dedup_library_hardlinked_extra_is_never_staged(world):
    """P21/A5: a hardlink to the canonical copy is byte-identical by
    definition and passes every confirmation tier, but staging it would not
    reclaim any space (the canonical link keeps the disk blocks alive) — it
    must be excluded, while a genuinely separate-storage twin still stages."""
    cfg = make_cfg(world, taxonomy=TAX)
    same = b"HARDLINK-CONTENT" * 40
    canonical = make_file(world["lib"] / "Video" / "Personal" / "G" / "x.mp4", same)
    linked = world["lib"] / "Videos" / "dumpA" / "x_link.mp4"
    linked.parent.mkdir(parents=True, exist_ok=True)
    os.link(str(canonical), str(linked))              # SAME inode as canonical
    separate = make_file(world["lib"] / "Videos" / "dumpB" / "x_copy.mp4", same)

    st = world["store"]
    for p, rel in ((canonical, "Video/Personal/G/x.mp4"),
                  (linked, "Videos/dumpA/x_link.mp4"),
                  (separate, "Videos/dumpB/x_copy.mp4")):
        size, qh = fingerprint.quick(str(p))
        st.index_upsert(rel.replace("/", os.sep), size, qh,
                        os.stat(p).st_mtime_ns, "seed")
    st.index_commit()
    run = st.start_run("seed", [], "cfg-test", "t")
    st.artifact_register("index:library", "index",
                         {"root": str(world["lib"])}, "cfg-test", run)

    res = planmod.build_dedup_library(st, cfg, under=["Videos"],
                                      drive_of=world["drive_of"])
    from mlo.report import read_plan
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 1                         # only the separate-storage copy
    assert rows[0]["src"].endswith(os.path.join("dumpB", "x_copy.mp4"))
    assert any("hardlink" in n.lower() for n in res.notes)


def test_dedup_library_extras_hardlinked_to_each_other_stage_only_one(world):
    """Two extras sharing an inode with EACH OTHER (not the canonical)
    represent only one unit of reclaimable storage — staging both would
    double-count it."""
    cfg = make_cfg(world, taxonomy=TAX)
    same = b"SHARED-EXTRA-INODE" * 30
    canonical = make_file(world["lib"] / "Video" / "Personal" / "G" / "x.mp4", same)
    extra_a = world["lib"] / "Videos" / "dumpA" / "a.mp4"
    make_file(extra_a, same)
    extra_b = world["lib"] / "Videos" / "dumpA" / "b_link.mp4"
    os.link(str(extra_a), str(extra_b))             # hardlinked to extra_a, not canonical

    st = world["store"]
    for p, rel in ((canonical, "Video/Personal/G/x.mp4"),
                  (extra_a, "Videos/dumpA/a.mp4"),
                  (extra_b, "Videos/dumpA/b_link.mp4")):
        size, qh = fingerprint.quick(str(p))
        st.index_upsert(rel.replace("/", os.sep), size, qh,
                        os.stat(p).st_mtime_ns, "seed")
    st.index_commit()
    run = st.start_run("seed", [], "cfg-test", "t")
    st.artifact_register("index:library", "index",
                         {"root": str(world["lib"])}, "cfg-test", run)

    res = planmod.build_dedup_library(st, cfg, under=["Videos"],
                                      drive_of=world["drive_of"])
    assert res.n_rows == 1                          # only ONE of a.mp4/b_link.mp4 stages
    from mlo.report import read_plan
    _, rows, _ = read_plan(res.path)
    assert rows[0]["src"].endswith(("a.mp4", "b_link.mp4"))


def test_stage_library_explicit_list(world):
    """Triage staging: an explicit list stages to <staging>/<label>/<relpath>;
    a path not in the index refuses the whole plan."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_with_content(world, {
        "Videos/w/promo.mp4": b"VENDOR" * 100,
        "Videos/w/keep.mp4": b"KEEP" * 100,
    })
    st = world["store"]
    res = planmod.build_stage_library(
        st, cfg, ["Videos/w/promo.mp4"], drive_of=world["drive_of"])
    assert res.n_rows == 1
    r = apply_plan(st, cfg, res.path, st.start_run("a", [], cfg.config_hash, "t"),
                   execute=True, drive_of=world["drive_of"])
    assert r.exit_code == 0 and r.counts == {"done": 1}
    assert (world["I"] / "Delete" / "triage" / "Videos" / "w" / "promo.mp4").exists()
    assert (world["lib"] / "Videos" / "w" / "keep.mp4").exists()
    left = {row["relpath"] for row in st.index_iter()}
    assert os.path.join("Videos", "w", "promo.mp4") not in left

    import pytest
    with pytest.raises(planmod.PlanError, match="not in the library index"):
        planmod.build_stage_library(st, cfg, ["Videos/w/nope.mp4"],
                                    drive_of=world["drive_of"])


def test_case_variant_occupied_destination_converges(world):
    """Review C17: a dest occupied by a case-variant twin must be recognized as
    occupied (stay put) — not planned into an eternal drift loop."""
    cfg = make_cfg(world, taxonomy=TAX)
    placed = "Video/Movies/Other/Inception (2010)/Inception (2010).mkv"
    dup = "Video/eSrc/inception (2010).mkv"     # lowercase: case-variant dest
    seed_library(world, [placed, dup])
    res = planmod.build_reorganize(world["store"], cfg, under=["Video"],
                                   drive_of=world["drive_of"])
    if os.name == "nt":
        assert res.n_rows == 0                  # occupied on a ci filesystem
        assert any("collisions (stay put): 1" in n for n in res.notes)
    else:
        assert res.n_rows in (0, 1)             # cs filesystems may move it


def test_prune_empty_removes_emptied_dirs_and_spares_kept(world):
    """rmdir_empty sweep: empty provenance dirs are pruned deepest-first; a dir
    still holding a file (a collision/junk that stayed) keeps its whole ancestor
    chain. The scoped root survives; re-plan converges to zero."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Photos/keepme/deep/stays.jpg"])
    lib = str(world["lib"])
    for d in ("Photos/empty/a/b", "Photos/mixed/gone"):
        os.makedirs(os.path.join(lib, d.replace("/", os.sep)), exist_ok=True)
    st = world["store"]

    res = planmod.build_prune_empty(st, cfg, under=["Photos"],
                                    drive_of=world["drive_of"])
    from mlo.report import read_plan
    _, rows, _ = read_plan(res.path)
    pruned = {os.path.relpath(r["dst"], lib) for r in rows}
    assert os.path.join("Photos", "empty", "a", "b") in pruned
    assert os.path.join("Photos", "empty") in pruned
    assert os.path.join("Photos", "mixed") in pruned
    # a directory holding a kept file, and the scoped root, are never pruned
    assert not any("keepme" in p for p in pruned)
    assert "Photos" not in pruned

    # execute cleanly (a clean run proves deepest-first: rmdir never hit a
    # non-empty dir), then re-plan converges to zero
    r = apply_plan(st, cfg, res.path,
                   st.start_run("p", [], cfg.config_hash, "t"),
                   execute=True, drive_of=world["drive_of"])
    assert r.exit_code == 0 and r.counts.get("done") == len(rows)
    res2 = planmod.build_prune_empty(st, cfg, under=["Photos"],
                                     drive_of=world["drive_of"])
    assert res2.n_rows == 0


def test_date_drain_places_photos_by_capture_date(world):
    """The same-name collision residue drains into Images/Photos/<year> named by
    capture time: a 13-digit epoch name uses its real date, others use mtime; the
    two colliding '02.jpg' land distinctly. Fingerprint twins are LEFT for dedup,
    never drained as if distinct. Re-plan converges."""
    import datetime
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Photos/dumpA/02.jpg", "Photos/dumpB/02.jpg",
                         "Photos/dumpC/1493582779771.jpg"])
    lib = world["lib"]
    st = world["store"]
    # a byte-identical twin pair (must NOT be date-drained — it's a dedup call)
    for rel in ("Photos/dumpE/twin.jpg", "Photos/dumpF/twin.jpg"):
        p = str(make_file(lib / rel, b"IDENTICAL-BYTES"))
        size, qh = fingerprint.quick(p)
        st.index_upsert(rel.replace("/", os.sep), size, qh,
                        os.stat(p).st_mtime_ns, "seed")

    def set_mtime(rel, dt):
        r = next(x for x in st.index_iter()
                 if x["relpath"] == rel.replace("/", os.sep))
        st.index_upsert(rel.replace("/", os.sep), r["size"], r["quick_hash"],
                        int(dt.timestamp() * 1e9), "seed")
    utc = datetime.timezone.utc
    set_mtime("Photos/dumpA/02.jpg", datetime.datetime(2021, 3, 15, 10, 0, 0, tzinfo=utc))
    set_mtime("Photos/dumpB/02.jpg", datetime.datetime(2022, 6, 1, 9, 30, 0, tzinfo=utc))
    st.index_commit()

    res = planmod.build_date_drain(st, cfg, under=["Photos"],
                                   drive_of=world["drive_of"])
    from mlo.report import read_plan
    _, rows, _ = read_plan(res.path)
    dsts = {os.path.relpath(r["dst"], str(lib)) for r in rows}
    assert os.path.join("Images", "Photos", "2021", "02_20210315_100000.jpg") in dsts
    assert os.path.join("Images", "Photos", "2022", "02_20220601_093000.jpg") in dsts
    ep = datetime.datetime.fromtimestamp(1493582779771 / 1000, utc)
    assert os.path.join("Images", "Photos", str(ep.year),
                        f"1493582779771_{ep.strftime('%Y%m%d_%H%M%S')}.jpg") in dsts
    assert not any("twin" in d for d in dsts)          # twins left for dedup
    assert res.n_rows == 3

    r = apply_plan(st, cfg, res.path,
                   st.start_run("d", [], cfg.config_hash, "t"),
                   execute=True, drive_of=world["drive_of"])
    assert r.exit_code == 0 and r.counts.get("done") == 3
    res2 = planmod.build_date_drain(st, cfg, under=["Photos"],
                                    drive_of=world["drive_of"])
    assert res2.n_rows == 0


def test_date_drain_scope_respects_taxonomy_route(world):
    """C32: date-drain must NOT touch files that taxonomy says aren't
    photo-shelf residue. The pre-C32 pass ripped album art out of Music,
    dumped UI graphics from Images\\Graphics_Icons\\ into Photos\\<year>\\,
    and flattened curated year/month/camera hierarchies — because date-drain
    only looked at the file extension. Now it consults taxonomy.route() and
    refuses to touch already-placed, sidecar, or non-photo files."""
    from dataclasses import replace
    from mlo.config import Layout
    # Use a realistic photos_root that matches a curated Images/Photos tree
    cfg = make_cfg(world, taxonomy={"Photos": (".jpg",), "Audio": (".mp3",)},
                   layout=replace(Layout(), photos_root="Images/Photos"))
    # Files that should STAY PUT under C32:
    seed_library(world, [
        "Audio/Music/English/AlbumArt_ABC.jpg",         # C18 cross-type sidecar
        "Images/Graphics_Icons/FILE000.JPG",            # canonical image home
        "Images/WhatsApp/IMG-20200318-WA0001.jpg",      # canonical image home
        "Images/Photos/2019/2019-07/Camera/foo.jpg",    # curated year tree
        # And one that SHOULD move (the intended date-drain target):
        "Photos/dumpA/needshome.jpg",                   # shelf residue
    ])
    st = world["store"]

    res = planmod.build_date_drain(st, cfg, drive_of=world["drive_of"])
    from mlo.report import read_plan
    _, rows, _ = read_plan(res.path)
    srcs = [r["src"] for r in rows]
    # Nothing that C32 protects should appear
    assert not any("AlbumArt" in s for s in srcs)
    assert not any("Graphics_Icons" in s for s in srcs)
    assert not any("WhatsApp" in s for s in srcs)
    assert not any(os.sep + "Camera" + os.sep in s for s in srcs)
    # The genuine shelf residue DOES appear
    assert any("needshome" in s for s in srcs)
    assert res.n_rows == 1
    assert any("C32" in note for note in res.notes)


def test_relocate_moves_mapped_files_with_guards(world):
    """plan relocate: an explicit critic-judged mapping moves files; identity
    rows drop (idempotent); a fingerprint twin stays (C21) even when mapped; an
    occupied destination stays (C17); a path missing from the index refuses the
    whole plan."""
    import pytest
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Presentations/Unsorted/arbit.pdf",
                         "Presentations/Unsorted/qdw.pdf",
                         "Documents/Quizzing/occupied.pdf"])
    lib = world["lib"]
    st = world["store"]
    # a byte-identical twin pair — mapped, but must stay (C21)
    for rel in ("Presentations/Unsorted/twin1.pdf", "Other/twin2.pdf"):
        p = str(make_file(lib / rel, b"SAME-BYTES"))
        size, qh = fingerprint.quick(p)
        st.index_upsert(rel.replace("/", os.sep), size, qh,
                        os.stat(p).st_mtime_ns, "seed")
    st.index_commit()

    m = {n("Presentations", "Unsorted", "arbit.pdf"):
             n("Documents", "Quizzing", "arbit.pdf"),
         n("Presentations", "Unsorted", "qdw.pdf"):
             n("Documents", "Quizzing", "occupied.pdf"),      # occupied -> stays
         n("Presentations", "Unsorted", "twin1.pdf"):
             n("Documents", "Quizzing", "twin1.pdf"),         # twin -> stays
         n("Documents", "Quizzing", "occupied.pdf"):
             n("Documents", "Quizzing", "occupied.pdf")}      # identity -> drop
    res = planmod.build_relocate(st, cfg, m, drive_of=world["drive_of"])
    from mlo.report import read_plan
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 1
    assert rows[0]["dst"] == os.path.join(cfg.library_root, "Documents",
                                          "Quizzing", "arbit.pdf")
    assert any("duplicate content (stay put): 1" in x for x in res.notes)
    assert any("collisions (stay put): 1" in x for x in res.notes)

    r = apply_plan(st, cfg, res.path,
                   st.start_run("m", [], cfg.config_hash, "t"),
                   execute=True, drive_of=world["drive_of"])
    assert r.exit_code == 0 and r.counts.get("done") == 1
    assert (lib / "Documents" / "Quizzing" / "arbit.pdf").exists()

    # a mapping naming a non-indexed path refuses outright
    with pytest.raises(planmod.PlanError, match="not in the library index"):
        planmod.build_relocate(st, cfg, {"nope/missing.pdf": "x/y.pdf"},
                               drive_of=world["drive_of"])
