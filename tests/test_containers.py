"""Semantic containers (C33): subtrees as first-class units.

The four-way triage: curated -> container -> dump -> loose. A container's
folder name declares its meaning; it moves WHOLE to its kind's home or not at
all. Matcher scope (D2/D3), destination scheme (D4), unit atomicity (D5), the
C21 twin exemption inside units (D6), protected refusal (D7), and the
route()/flatten guards (D8) are all pinned here.
"""
from __future__ import annotations

import os

import pytest

from conftest import make_file
from helpers import make_cfg
from mlo import containers, fingerprint, plan as planmod
from mlo.apply import apply_plan
from mlo.plan import PlanError
from mlo.report import read_plan
from mlo.taxonomy import route

TAX = {"Video": (".mp4", ".mkv"), "Audio": (".mp3",),
       "Photos": (".jpg",), "Documents": (".pdf", ".txt")}


def seed_library(world, rels: list[str], content_by_rel=None) -> None:
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


# ── the matcher ──────────────────────────────────────────────────────────────

def test_matcher_finds_containers_at_any_depth(world):
    cfg = make_cfg(world, taxonomy=TAX)
    m = containers.root_of(cfg, n("Documents", "user1", "Phone Backups",
                                  "S5", "Phone", "Contacts_005.vcf"))
    assert m is not None
    assert m.kind == "phone-backup"
    assert m.root == n("Documents", "user1", "Phone Backups")
    assert m.home == n("Backups", "Phones")
    # depth 1 (directly under the bucket)
    m2 = containers.root_of(cfg, n("Documents", "User1Backup", "notes.txt"))
    assert m2 is not None and m2.kind == "app-backup"


def test_matcher_media_tops_are_out_of_scope(world):
    """D2: media taxonomy owns its territory — same boundary as C28."""
    cfg = make_cfg(world, taxonomy=TAX)
    for rel in (n("Audio", "PhoneBackup", "song.mp3"),
                n("Photos", "Phone Backups", "x.jpg"),
                n("Video", "User1Backup", "clip.mp4")):
        assert containers.root_of(cfg, rel) is None


def test_matcher_outermost_wins_within_kind(world):
    """D3 (post-correction #4): outermost-wins within one kind. Two phone
    backups in the same path — the outer one is the unit; the inner is one
    of its folders."""
    cfg = make_cfg(world, taxonomy=TAX)
    m = containers.root_of(cfg,
                           n("Documents", "user1", "Phone Backups",
                             "Phone Backups", "x.txt"))
    assert m is not None
    assert m.kind == "phone-backup"
    assert m.root == n("Documents", "user1", "Phone Backups")


def test_matcher_kind_priority_phone_beats_drive(world):
    """D3 (owner correction #4): phone-backup outranks drive-image regardless
    of position. A phone backup nested in a drive image is a phone backup."""
    cfg = make_cfg(world, taxonomy=TAX)
    m = containers.root_of(cfg, n("Documents", "D drive", "Phone backup",
                                  "x.txt"))
    assert m is not None
    assert m.kind == "phone-backup"
    assert m.root == n("Documents", "D drive", "Phone backup")


def test_matcher_drops_all_context_between_bucket_and_root(world):
    """D4 (owner correction): EVERYTHING between the bucket and the container
    root is provenance — drive prefixes (I_SSD1) and person names
    (user1) alike. Any shape of the same logical container computes the SAME
    home + root identity, so scattered fragments reunite."""
    cfg = make_cfg(world, taxonomy=TAX)
    shapes = [
        n("Documents", "I_SSD1", "user1", "Phone Backups", "S5", "x.vcf"),
        n("Documents", "user1", "Phone Backups", "S5", "x.vcf"),
        n("Other", "user1", "Phone Backups", "S5", "x.vcf"),
    ]
    matches = [containers.root_of(cfg, s) for s in shapes]
    assert all(m is not None for m in matches)
    assert {m.kind for m in matches} == {"phone-backup"}
    assert {m.home for m in matches} == {n("Backups", "Phones")}
    assert {os.path.basename(m.root) for m in matches} == {"Phone Backups"}


def test_matcher_config_patterns_extend_builtins(world):
    from dataclasses import replace
    cfg = make_cfg(world, taxonomy=TAX)
    cfg = replace(cfg, container_patterns={"dcim-roll": (r"^dcim$",)},
                  container_homes={"dcim-roll": "Images/Photos/Unsorted"})
    m = containers.root_of(cfg, n("Documents", "old phone", "DCIM",
                                  "IMG_001.jpg"))
    assert m is not None and m.kind == "dcim-roll"
    assert m.home == n("Images", "Photos", "Unsorted")


def test_config_rejects_bad_container_surface(tmp_path):
    from mlo.config import ConfigError, load
    base = ("[library]\nroot = 'C:/lib'\n"
            "[taxonomy.buckets]\nDocuments = ['.pdf']\n")
    bad_rx = base + "[containers.patterns]\nphone-backup = ['[unclosed']\n"
    p = tmp_path / "a.toml"
    p.write_text(bad_rx, encoding="utf-8")
    with pytest.raises(ConfigError, match="bad regex"):
        load(str(p))
    no_home = base + "[containers.patterns]\nmystery = ['^x$']\n"
    p2 = tmp_path / "b.toml"
    p2.write_text(no_home, encoding="utf-8")
    with pytest.raises(ConfigError, match="no destination"):
        load(str(p2))


# ── the builder: D10 device-keying, D11 merge, D12 dedup/disambiguate ────────

def test_phone_backup_is_device_keyed(world):
    """D10: destination is Backups\\Phones\\<device>\\<below-device>. The
    container's own name ('Phone Backups') and any owner segment ('user1') are
    provenance and do NOT appear when there's no clash."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, [
        "Documents/user1/Phone Backups/S5/Phone/Contacts_005.vcf",
        "Documents/user1/Phone Backups/S5/Phone/Contacts_006.vcf",
    ])
    st = world["store"]
    res = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    lib = cfg.library_root
    dsts = {r["dst"] for r in rows}
    assert os.path.join(lib, "Backups", "Phones", "S5", "Phone",
                        "Contacts_005.vcf") in dsts
    assert os.path.join(lib, "Backups", "Phones", "S5", "Phone",
                        "Contacts_006.vcf") in dsts
    assert not any("user1" in d for d in dsts)
    assert not any("Phone Backups" in d for d in dsts)
    assert res.n_rows == 2


def test_device_key_normalizes_variants(world):
    """S4backup, s5, Nexus6 — different segments the same device family. The
    matcher extracts + normalizes so scattered backups of one device merge."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, [
        "Documents/user2/Phone Backups/S4backup/db.txt",
        "Backups/WhatsApp/E_NAS1/User3/CellPhone Backups/S4backup/db2.txt",
        "Documents/User2/User1Backup/Nexus6/bt/log.txt",
    ])
    st = world["store"]
    res = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    lib = cfg.library_root
    dsts = {r["dst"] for r in rows}
    # S4backup normalizes to S4; both files land in the same S4 tree
    assert os.path.join(lib, "Backups", "Phones", "S4", "db.txt") in dsts
    assert os.path.join(lib, "Backups", "Phones", "S4", "db2.txt") in dsts
    # Nexus6 preserved as-is
    assert os.path.join(lib, "Backups", "Phones", "Nexus6", "bt",
                        "log.txt") in dsts


def test_fragments_merge_across_source_containers(world):
    """D11: files from different source containers with no path collision
    merge into the same device tree — the whole point of device-keying."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, [
        "Documents/user1/Phone Backups/S5/Phone/Contacts.vcf",
        "Other/User2/User1Backup/S5/SD Card/font.ttf",
        "Documents/User2/User1Backup/Nexus6/notes.txt",
    ])
    st = world["store"]
    res = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    lib = cfg.library_root
    dsts = {r["dst"] for r in rows}
    assert os.path.join(lib, "Backups", "Phones", "S5", "Phone",
                        "Contacts.vcf") in dsts
    assert os.path.join(lib, "Backups", "Phones", "S5", "SD Card",
                        "font.ttf") in dsts
    assert os.path.join(lib, "Backups", "Phones", "Nexus6",
                        "notes.txt") in dsts
    # Neither container-name nor owner appears anywhere (no clash → no
    # provenance survives)
    assert not any("User1Backup" in d for d in dsts)
    assert not any("User1Backup" in d for d in dsts)


def test_byte_identical_collision_dedups(world):
    """D12: byte-identical files targeting the same device slot dedup — one
    physical copy survives, others are silently skipped ('already there')."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, [
        "Documents/user1/Phone Backups/S5/Phone/Contacts.vcf",
        "Other/user2/Phone Backups/S5/Phone/Contacts.vcf",
    ], content_by_rel={
        "Documents/user1/Phone Backups/S5/Phone/Contacts.vcf": b"SAME",
        "Other/user2/Phone Backups/S5/Phone/Contacts.vcf": b"SAME",
    })
    st = world["store"]
    res = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 1                           # only one survives
    _, rows, _ = read_plan(res.path)
    assert rows[0]["dst"].endswith(os.path.join(
        "Backups", "Phones", "S5", "Phone", "Contacts.vcf"))
    # no owner in dest (byte-identical -> no clash, just dedup)
    assert "user1" not in rows[0]["dst"] and "user2" not in rows[0]["dst"]
    assert any("dedup skipped" in note for note in res.notes)


def test_content_different_collision_owner_disambiguates(world):
    """D12: two files with DIFFERENT content targeting the same device slot
    each get an owner discriminator — content is preserved, and the collision
    is transparent in the destination path."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, [
        "Documents/user1/Phone Backups/S5/Phone/Contacts.vcf",
        "Other/user2/Phone Backups/S5/Phone/Contacts.vcf",
    ], content_by_rel={
        "Documents/user1/Phone Backups/S5/Phone/Contacts.vcf": b"USER1",
        "Other/user2/Phone Backups/S5/Phone/Contacts.vcf": b"USER2",
    })
    st = world["store"]
    res = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    lib = cfg.library_root
    dsts = {r["dst"] for r in rows}
    assert res.n_rows == 2
    assert os.path.join(lib, "Backups", "Phones", "S5", "user1", "Phone",
                        "Contacts.vcf") in dsts
    assert os.path.join(lib, "Backups", "Phones", "S5", "user2", "Phone",
                        "Contacts.vcf") in dsts
    assert any("disambiguated" in note for note in res.notes)


def test_existing_target_content_matches_source_dedups(world):
    """An existing library file at the target with the SAME fingerprint as an
    incoming source: the source is 'already there' (dedup skip). No plan
    row."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, [
        "Documents/user1/Phone Backups/S5/Phone/Contacts.vcf",
        "Backups/Phones/S5/Phone/Contacts.vcf",
    ], content_by_rel={
        "Documents/user1/Phone Backups/S5/Phone/Contacts.vcf": b"SAME",
        "Backups/Phones/S5/Phone/Contacts.vcf": b"SAME",
    })
    st = world["store"]
    res = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 0
    assert any("dedup skipped" in note for note in res.notes)


def test_existing_target_content_differs_owner_disambiguates(world):
    """An existing target with DIFFERENT content: the source moves to an
    owner-disambiguated slot instead of colliding."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, [
        "Documents/user1/Phone Backups/S5/Phone/Contacts.vcf",
        "Backups/Phones/S5/Phone/Contacts.vcf",
    ], content_by_rel={
        "Documents/user1/Phone Backups/S5/Phone/Contacts.vcf": b"USER1",
        "Backups/Phones/S5/Phone/Contacts.vcf": b"OTHER",
    })
    st = world["store"]
    res = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 1
    assert rows[0]["dst"].endswith(os.path.join(
        "Backups", "Phones", "S5", "user1", "Phone", "Contacts.vcf"))


def test_container_root_named_after_device(world):
    """A container whose ROOT segment names a device ('S5 backup') provides
    the device directly — no first-level device child is needed."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Documents/user2/S5 backup/Phone/Contacts.vcf"])
    st = world["store"]
    res = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    lib = cfg.library_root
    assert res.n_rows == 1
    assert rows[0]["dst"] == os.path.join(
        lib, "Backups", "Phones", "S5", "Phone", "Contacts.vcf")


def test_phone_backup_without_device_lands_in_unsorted(world):
    """When no device segment can be extracted, the file lands under
    <home>\\Unsorted\\<path-below-container>. The container's own name is
    provenance too — dropped, not preserved as a Unknown-subfolder — matching
    the Music\\Unsorted / Photos\\Unsorted convention."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world,
                 ["Documents/user1/Phone Backups/some_folder/loose.txt"])
    st = world["store"]
    res = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    lib = cfg.library_root
    assert res.n_rows == 1
    assert rows[0]["dst"] == os.path.join(
        lib, "Backups", "Phones", "Unsorted", "some_folder", "loose.txt")
    assert "Phone Backups" not in rows[0]["dst"]


def test_phone_backup_beats_drive_image_when_nested(world):
    """Owner correction #4: a phone-backup nested inside a drive-image (the
    `D drive\\Phone backup\\WhatsApp Documents\\…` case) is a phone backup,
    not a drive image with a phone folder. Kind priority (phone > drive)
    overrides outermost-position selection."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world,
                 ["Documents/D drive/Phone backup/WhatsApp Documents/"
                  "Sent/purple_carrot.txt"])
    st = world["store"]
    res = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    lib = cfg.library_root
    assert res.n_rows == 1
    assert rows[0]["dst"] == os.path.join(
        lib, "Backups", "Phones", "Unsorted", "WhatsApp Documents",
        "Sent", "purple_carrot.txt")
    assert "Drives" not in rows[0]["dst"]
    assert "D drive" not in rows[0]["dst"]


def test_plain_drive_image_still_matches_drive_kind(world):
    """A drive image with NO phone-backup inside is still a drive image."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world,
                 ["Documents/D drive/Windows/System32/config.txt"])
    st = world["store"]
    res = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    lib = cfg.library_root
    assert res.n_rows == 1
    assert rows[0]["dst"] == os.path.join(
        lib, "Backups", "Drives", "D drive", "Windows", "System32",
        "config.txt")


def test_app_backup_keeps_container_name_scheme(world):
    """Non-phone kinds (app-backup, drive-image) keep the container-name
    scheme: <home>\\<container-name>\\<internal>. Only phone-backup is
    device-keyed."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Documents/ProjectBackup/src/main.txt"])
    st = world["store"]
    res = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    lib = cfg.library_root
    assert res.n_rows == 1
    assert rows[0]["dst"] == os.path.join(
        lib, "Backups", "Apps", "ProjectBackup", "src", "main.txt")


def test_kind_refinement_uses_find_device(world):
    """A generic '*Backup' container whose first-level child is a device
    (S5/, Nexus6/) refines to phone-backup and lands in Backups\\Phones. A
    generic '*Backup' container whose children are apps stays app-backup."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, [
        "Other/User2/User1Backup/S5/SD Card/font.ttf",       # device -> phone
        "Documents/User2/User1Backup/Nexus6/notes.txt",      # device -> phone
        "Documents/ProjectBackup/src/main.txt",            # app-ish -> Apps
    ])
    st = world["store"]
    res = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    rules = {r["reason"]["rule"] for r in rows}
    # Phone-backups cluster by DEVICE across sources (S5 groups User1Backup's
    # font.ttf into the same tree as any other S5 file); User1Backup's Nexus6
    # becomes the Nexus6 device. app-backup keeps container-name key.
    assert "container:phone-backup:S5" in rules
    assert "container:phone-backup:Nexus6" in rules
    assert "container:app-backup:ProjectBackup" in rules


def test_at_home_is_idempotent(world):
    """Files at their home path produce no rows. C39: BOTH kinds are now
    CLAIMED at home by root_of's at-home rule (`<home>\\<ident>\\…`), not
    merely invisible — the phone-backup file counts as 'already at home'
    right alongside the app-backup, and every per-file mechanism sees them
    as container members."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Backups/Phones/S5/Phone/x.vcf",
                         "Backups/Apps/User1Backup/notes.txt"])
    st = world["store"]
    res = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 0
    assert any("already at home: 2" in note for note in res.notes)


def test_protected_inside_container_refuses_whole_plan(world):
    """D7/L12: protected content inside a container is exactly when a human
    must look — the whole plan refuses to build."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Documents/User1Backup/bluestacks_settings.txt"])
    st = world["store"]
    with pytest.raises(PlanError, match="protected"):
        planmod.build_containers(st, cfg, drive_of=world["drive_of"])


def test_under_scoping(world):
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Documents/User1Backup/a.txt",
                         "Other/User1Backup/b.txt"])
    st = world["store"]
    res = planmod.build_containers(st, cfg, under=["Documents"],
                                   drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    assert res.n_rows == 1
    assert "User1Backup" in rows[0]["src"]


def test_execute_then_converge_to_zero(world):
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Documents/user1/Phone Backups/S5/Phone/x.vcf"])
    st = world["store"]
    res = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 1
    r = apply_plan(st, cfg, res.path,
                   st.start_run("c", [], cfg.config_hash, "t"),
                   execute=True, drive_of=world["drive_of"])
    assert r.exit_code == 0 and r.counts == {"done": 1}
    res2 = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    assert res2.n_rows == 0


# ── find_device / owner_of unit coverage ─────────────────────────────────────

def test_find_device_normalizes_common_variants():
    assert containers.find_device("S5") == "S5"
    assert containers.find_device("s5") == "S5"
    assert containers.find_device("S5backup") == "S5"
    assert containers.find_device("S5 backup") == "S5"
    assert containers.find_device("Note10") == "Note10"
    assert containers.find_device("Note 10") == "Note10"
    assert containers.find_device("Nexus6") == "Nexus6"
    assert containers.find_device("Pixel8Pro") == "Pixel8Pro"
    assert containers.find_device("iPhone11Pro") == "iPhone11Pro"
    assert containers.find_device("iPad Mini") == "iPad Mini"
    # negative controls
    assert containers.find_device("Phone Backups") is None
    assert containers.find_device("User1Backup") is None
    assert containers.find_device("SD Card") is None
    assert containers.find_device("user1") is None


def test_owner_of_extracts_person_segment():
    assert containers.owner_of(None, n("Documents", "user1", "Phone Backups")) \
        == "user1"
    # provenance drive-prefix is skipped
    assert containers.owner_of(
        None, n("Documents", "I_SSD1", "user1", "Phone Backups")) \
        == "user1"
    # last non-provenance in reverse; provenance-heavy path
    assert containers.owner_of(
        None,
        n("Backups", "WhatsApp", "E_NAS1", "User3", "CellPhone Backups")) \
        == "User3"
    # no person segment
    assert containers.owner_of(None, n("Documents", "Phone Backups")) is None


# ── the guards (D8) ──────────────────────────────────────────────────────────

def test_route_container_member_stays_put(world):
    cfg = make_cfg(world, taxonomy=TAX)
    # a MEDIA file inside a container must not be routed individually
    rel = n("Documents", "User1Backup", "DCIM", "IMG_001.jpg")
    r = route(cfg, rel)
    assert r is not None
    assert r.dest_relpath == rel
    assert r.rule == "route:container:member"


def test_reorganize_and_date_drain_leave_container_files_alone(world):
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Documents/User1Backup/holiday.jpg",
                         "Documents/User1Backup/song.mp3"])
    st = world["store"]
    reorg = planmod.build_reorganize(st, cfg, drive_of=world["drive_of"])
    drain = planmod.build_date_drain(st, cfg, drive_of=world["drive_of"])
    for res in (reorg, drain):
        if not res.n_rows:
            continue
        _, rows, _ = read_plan(res.path)
        assert not any("User1Backup" in r["src"] for r in rows)


def test_flatten_skips_container_subtrees(world):
    """flatten never strips a segment that is (or wraps) a container — the
    backup-named wrapper is a snapshot, not a dump. Without this check,
    _PROVENANCE_SEG (which matches 'backup') would scatter backups flat."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, ["Documents/User1Backup/inner/file.txt",
                         "Documents/E_NAS1/loose.pdf"])
    st = world["store"]
    res = planmod.build_flatten_provenance(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    srcs = {r["src"] for r in rows}
    assert not any("User1Backup" in s for s in srcs)      # container: skipped
    assert any("E_NAS1" in s for s in srcs)            # true dump: flattened
    assert any("container member (C33, stay put): 1" in note
               for note in res.notes)


# ── C39: the container HOME is durable ───────────────────────────────────────
# Live failure (2026-07-12): once `…\Phone Backups\…` was consolidated to
# `Backups\Phones\Nexus6\…`, no phone-backup pattern matched the new path —
# so reorganize (C35 routes) tore the S5 APKs out to Installers\, flatten
# (C34) stripped the nested `Atrix Backup` segment, and build_containers
# itself re-claimed the inner `InkPad_Notepad\backup` folders as app-backups,
# comingling two apps' data in `Backups\Apps\backup\` (19 files, later lost).
# root_of now claims any `<home>\<ident>\…` path FIRST, so every per-file
# mechanism sees at-home members as container members.

def test_c39_root_of_claims_at_home_paths(world):
    cfg = make_cfg(world, taxonomy=TAX)
    m = containers.root_of(cfg, n("Backups", "Phones", "S5", "DCIM", "x.jpg"))
    assert m is not None
    assert m.kind == "phone-backup"
    assert m.root == n("Backups", "Phones", "S5")
    m2 = containers.root_of(cfg, n("Backups", "Drives", "D drive", "y.txt"))
    assert m2 is not None and m2.kind == "drive-image"
    assert m2.root == n("Backups", "Drives", "D drive")
    # a file DIRECTLY under the home (no ident dir) is not claimed
    assert containers.root_of(cfg, n("Backups", "Phones", "loose.jpg")) is None


def test_c39_route_stays_put_at_home_even_for_media(world):
    """The live tear-out vector: media/bucketed files AT the device-keyed
    home must route as container members, not per-file."""
    cfg = make_cfg(world, taxonomy=TAX)
    for rel in (n("Backups", "Phones", "S5", "Music", "song.mp3"),
                n("Backups", "Phones", "S5", "DCIM", "1428149400000.jpg"),
                n("Backups", "Phones", "S5", "Movies", "Inception (2010).mp4")):
        r = route(cfg, rel)
        assert r is not None
        assert r.rule == "route:container:member"
        assert r.dest_relpath == rel


def test_c39_reorganize_never_tears_out_of_home(world):
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, [
        "Backups/Phones/S5/Music/song.mp3",
        "Backups/Phones/S5/DCIM/1428149400000.jpg",
        "Backups/Phones/S5/Movies/Inception (2010).mp4",
    ])
    st = world["store"]
    res = planmod.build_reorganize(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 0


def test_c39_inner_backup_folders_not_reclaimed(world):
    """The InkPad/ovuview live failure: app-data `backup` folders INSIDE an
    at-home phone snapshot must not be re-claimed as separate app-backup
    containers and moved out."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, [
        "Backups/Phones/Nexus6/InkPad_Notepad/backup/1/note.txt",
        "Backups/Phones/Nexus6/ovuview/backup/backup-main-d.txt",
    ])
    st = world["store"]
    res = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 0
    assert any("already at home: 2" in note for note in res.notes)


def test_c39_flatten_skips_at_home_nested_backup_segment(world):
    """The Atrix Backup live failure: a backup-named segment nested inside an
    at-home snapshot is snapshot structure, never a provenance wrapper."""
    cfg = make_cfg(world, taxonomy=TAX)
    seed_library(world, [
        "Backups/Phones/S5/SD Card/Atrix Backup/download/img.aspx",
    ])
    st = world["store"]
    res = planmod.build_flatten_provenance(st, cfg, drive_of=world["drive_of"])
    assert res.n_rows == 0


def test_c39_dedup_never_cherry_picks_a_container_member(world):
    """D6: the copy inside a snapshot is part of the unit's integrity — the
    loose twin stages, the container copy is the one that stays."""
    cfg = make_cfg(world, taxonomy=TAX)
    same = b"TWIN-CONTENT" * 400
    seed_library(world, [
        "Backups/Phones/S5/DCIM/photo.jpg",
        "Documents/loose/photo.jpg",
    ], content_by_rel={
        "Backups/Phones/S5/DCIM/photo.jpg": same,
        "Documents/loose/photo.jpg": same,
    })
    st = world["store"]
    res = planmod.build_dedup_library(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    assert len(rows) == 1
    assert n("Documents", "loose", "photo.jpg") in rows[0]["src"]
    assert "Backups" not in rows[0]["src"]
    # the container copy is named as the canonical keeper
    assert "dup:keep:" + n("Backups", "Phones", "S5", "DCIM", "photo.jpg") \
        == rows[0]["reason"]["rule"]


def test_c39_disambiguated_dest_dedups_byte_identical_occupant(world):
    """Validator-A finding: an equal-fingerprint occupant at the
    owner-disambiguated destination must dedup-skip, not emit a row that
    drifts forever on 'destination occupied'."""
    cfg = make_cfg(world, taxonomy=TAX)
    same = b"SAME-BYTES" * 300
    seed_library(world, [
        # incoming container member (owner 'user1')
        "Documents/user1/Phone Backups/S5/photo.jpg",
        # different content already at the naive dest -> forces disambiguation
        "Backups/Phones/S5/photo.jpg",
        # byte-identical occupant already AT the disambiguated dest
        "Backups/Phones/S5/user1/photo.jpg",
    ], content_by_rel={
        "Documents/user1/Phone Backups/S5/photo.jpg": same,
        "Backups/Phones/S5/photo.jpg": b"DIFFERENT" * 300,
        "Backups/Phones/S5/user1/photo.jpg": same,
    })
    st = world["store"]
    res = planmod.build_containers(st, cfg, drive_of=world["drive_of"])
    _, rows, _ = read_plan(res.path)
    assert not any("user1" in r["src"] and "Documents" in r["src"]
                   for r in rows), \
        "byte-identical disamb occupant must dedup-skip, not plan"
