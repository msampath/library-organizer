"""P17 (Ebooks/C43) end-to-end: reorganize moves a real synthetic epub home,
unsorted books join the review list, sidecars follow, hints round-trip the
book_* fields, config surfaces the ebooks_root, and both shipped templates
carry the Ebooks bucket."""
from __future__ import annotations

import json
import os
import zipfile

from conftest import make_file
from helpers import make_cfg
from mlo import fingerprint, hints as hintsmod, plan as planmod, report
from mlo.config import ConfigError, Layout, load
from mlo.taxonomy import Hints

TAX = {"Documents": (".pdf", ".txt"), "Ebooks": (".epub", ".mobi", ".lit")}


def n(*segs):
    return os.sep.join(segs)


def _write_epub(path, title="Dune", author="Frank Herbert"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>""")
        z.writestr("OEBPS/content.opf", f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
  </metadata>
</package>""")


def seed_library(world, rels: list[str]) -> None:
    st = world["store"]
    for rel in rels:
        p = world["lib"] / rel
        if p.suffix == ".epub" and not p.exists():
            _write_epub(p)
        elif not p.exists():
            make_file(p, rel.encode() * 3)
        size, qh = fingerprint.quick(str(p))
        st.index_upsert(rel.replace("/", os.sep), size, qh,
                        os.stat(p).st_mtime_ns, "seed")
    st.index_commit()
    run = st.start_run("seed", [], "cfg-test", "t")
    st.artifact_register("index:library", "index",
                         {"root": str(world["lib"])}, "cfg-test", run)


# ── end-to-end: reorganize moves a real synthetic epub home ─────────────────

def test_reorganize_moves_synthetic_epub_out_of_documents_unsorted(world):
    cfg = make_cfg(world, taxonomy=TAX)
    rel = "Documents/Unsorted/some garbage rip name 42.epub"
    seed_library(world, [rel])
    st = world["store"]

    lib_hints = hintsmod.load_hints(None)
    lib_hints = hintsmod.augment_bookmeta_library(cfg, st, [], lib_hints)
    res = planmod.build_reorganize(st, cfg, hints=lib_hints,
                                   drive_of=world["drive_of"])
    _, rows, _ = report.read_plan(res.path)
    dests = {r["dst"] for r in rows}
    assert os.path.join(cfg.library_root, "Books", "Herbert, Frank",
                        "Dune.epub") in dests


def test_bookmeta_augment_fills_only_when_absent(world):
    """The fill-only-when-absent discipline (augment_exif_library precedent):
    a hint that already carries book_author is never overwritten."""
    cfg = make_cfg(world, taxonomy=TAX)
    rel = "Documents/Unsorted/x.epub"
    seed_library(world, [rel])
    st = world["store"]
    pre = {n("Documents", "Unsorted", "x.epub"): Hints(book_author="Preset, Author")}
    out = hintsmod.augment_bookmeta_library(cfg, st, [], dict(pre))
    assert out[n("Documents", "Unsorted", "x.epub")].book_author == "Preset, Author"


# ── unsorted books join the review list ──────────────────────────────────────

def test_unsorted_rule_rows_enter_review_list(world):
    cfg = make_cfg(world, taxonomy=TAX)
    rel = "Other/Unsorted/AdventuresOfHuckleberryFinn.lit"
    seed_library(world, [rel])
    st = world["store"]
    res = planmod.build_reorganize(st, cfg, under=["Other"],
                                   drive_of=world["drive_of"])
    assert n("Other", "Unsorted", "AdventuresOfHuckleberryFinn.lit") \
        in res.unrouted


# ── sidecars follow their book anchor ────────────────────────────────────────

def test_metadata_opf_and_cover_sidecars_follow_the_book(world):
    cfg = make_cfg(world, taxonomy=TAX)
    book = "Documents/Unsorted/z book.epub"
    opf = "Documents/Unsorted/metadata.opf"
    cover = "Documents/Unsorted/cover.jpg"
    seed_library(world, [book, opf, cover])
    st = world["store"]
    lib_hints = hintsmod.load_hints(None)
    lib_hints = hintsmod.augment_bookmeta_library(cfg, st, [], lib_hints)
    res = planmod.build_reorganize(st, cfg, hints=lib_hints,
                                   drive_of=world["drive_of"])
    _, rows, _ = report.read_plan(res.path)
    dst_folder = os.path.join(cfg.library_root, "Books", "Herbert, Frank")
    dests = {r["dst"] for r in rows}
    assert os.path.join(dst_folder, "Dune.epub") in dests
    assert os.path.join(dst_folder, "metadata.opf") in dests
    assert os.path.join(dst_folder, "cover.jpg") in dests


# ── hints round-trip: book keys accepted, typo keys refused ─────────────────

def test_hints_round_trip_book_keys(tmp_path):
    p = tmp_path / "hints.json"
    p.write_text(json.dumps({
        "Books/Unsorted/x.lit": {
            "book_author": "Twain, Mark", "book_title": "Huckleberry Finn",
            "book_series": None, "book_index": None,
        }
    }), encoding="utf-8")
    out = hintsmod.load_hints(str(p))
    h = out[os.path.join("Books", "Unsorted", "x.lit")]
    assert h.book_author == "Twain, Mark"
    assert h.book_title == "Huckleberry Finn"


def test_hints_typo_book_key_refused(tmp_path):
    p = tmp_path / "hints.json"
    p.write_text(json.dumps({"x.epub": {"book_authr": "Typo"}}), encoding="utf-8")
    try:
        hintsmod.load_hints(str(p))
        assert False, "expected ConfigError"
    except ConfigError:
        pass


def test_hints_book_index_must_be_nonnegative_int(tmp_path):
    p = tmp_path / "hints.json"
    p.write_text(json.dumps({"x.epub": {"book_index": -1}}), encoding="utf-8")
    try:
        hintsmod.load_hints(str(p))
        assert False, "expected ConfigError"
    except ConfigError:
        pass


def test_pilot_hints_jsonable_round_trips_book_fields():
    from mlo.pilot import _hints_jsonable
    hmap = {"a/b.epub": Hints(book_author="Herbert, Frank", book_title="Dune",
                              book_series="Dune", book_index=1)}
    doc = _hints_jsonable(hmap)
    assert doc["a/b.epub"] == {"book_author": "Herbert, Frank",
                               "book_title": "Dune", "book_series": "Dune",
                               "book_index": 1}

# ── a merged subagent hints file loads and re-routes ─────────────────────────

def test_merged_subagent_hints_file_reroutes_unsorted_book(world):
    """Simulates the Opus-subagent protocol's output artifact: a hints-JSON
    fragment naming an author for a title-only book, validated by load_hints,
    then consumed by a re-plan (mlo pilot --hints)."""
    cfg = make_cfg(world, taxonomy=TAX)
    rel = "Books/Unsorted/AdventuresOfHuckleberryFinn.lit"
    seed_library(world, [rel])
    st = world["store"]

    import tempfile
    hpath = os.path.join(tempfile.mkdtemp(), "subagent-hints.json")
    with open(hpath, "w", encoding="utf-8") as f:
        json.dump({rel: {"book_author": "Twain, Mark"}}, f)

    lib_hints = hintsmod.load_hints(hpath)
    res = planmod.build_reorganize(st, cfg, hints=lib_hints,
                                   drive_of=world["drive_of"])
    _, rows, _ = report.read_plan(res.path)
    dests = {r["dst"] for r in rows}
    assert os.path.join(cfg.library_root, "Books", "Twain, Mark",
                        "AdventuresOfHuckleberryFinn.lit") in dests


# ── config: ebooks_root default/override ─────────────────────────────────────

def test_layout_ebooks_root_default():
    assert Layout().ebooks_root == "Books"


def test_config_load_accepts_custom_ebooks_root(tmp_path):
    cfg_path = tmp_path / "mlo.toml"
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg_path.write_text(f"""
[library]
root = {json.dumps(str(lib))}

[layout]
ebooks_root = "Media/Books"
""", encoding="utf-8")
    cfg = load(str(cfg_path))
    assert cfg.layout.ebooks_root == "Media/Books"


def test_config_rejects_absolute_ebooks_root(tmp_path):
    cfg_path = tmp_path / "mlo.toml"
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg_path.write_text(f"""
[library]
root = {json.dumps(str(lib))}

[layout]
ebooks_root = "C:\\\\Books"
""", encoding="utf-8")
    try:
        load(str(cfg_path))
        assert False, "expected ConfigError"
    except ConfigError:
        pass


# ── both shipped templates contain the Ebooks bucket ─────────────────────────

def test_starter_config_has_ebooks_bucket_and_no_epub_in_documents():
    assert "Ebooks" in report.STARTER_CONFIG
    assert ".epub" in report.STARTER_CONFIG   # present, but not in Documents
    doc_line = next(ln for ln in report.STARTER_CONFIG.splitlines()
                    if ln.strip().startswith("Documents "))
    assert ".epub" not in doc_line


def test_generated_config_has_ebooks_bucket_and_no_epub_in_documents():
    text = report.render_generated_config("X:\\Organized", "src", "E:\\")
    assert "Ebooks" in text
    doc_line = next(ln for ln in text.splitlines()
                    if ln.strip().startswith("Documents "))
    assert ".epub" not in doc_line
