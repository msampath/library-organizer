"""bookmeta: epub/mobi embedded readers, filename parsing, author shelving,
segment sanitizing. Parsers get hypothesis property tests (v2 lesson)."""
from __future__ import annotations

import os
import struct
import zipfile

from hypothesis import given, strategies as st

from mlo import bookmeta


# ── epub_meta ────────────────────────────────────────────────────────────────

def _write_epub(path, title="The Hitchhiker's Guide to the Galaxy",
                author="Douglas Adams", series=None, series_index=None,
                language="en"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>""")
        series_meta = ""
        if series:
            series_meta += (
                f'<meta name="calibre:series" content="{series}"/>\n')
        if series_index is not None:
            series_meta += (
                f'<meta name="calibre:series_index" content="{series_index}"/>\n')
        z.writestr("OEBPS/content.opf", f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:language>{language}</dc:language>
    {series_meta}
  </metadata>
</package>""")


def test_epub_meta_extracts_title_author_language(tmp_path):
    p = tmp_path / "book.epub"
    _write_epub(p)
    m = bookmeta.epub_meta(str(p))
    assert m["title"] == "The Hitchhiker's Guide to the Galaxy"
    assert m["author"] == "Douglas Adams"
    assert m["language"] == "en"
    assert m["series"] is None


def test_epub_meta_extracts_calibre_series(tmp_path):
    p = tmp_path / "book.epub"
    _write_epub(p, series="Hitchhiker's Guide", series_index="1")
    m = bookmeta.epub_meta(str(p))
    assert m["series"] == "Hitchhiker's Guide"
    assert m["series_index"] == 1


def test_epub_meta_malformed_zip_returns_none(tmp_path):
    p = tmp_path / "bad.epub"
    p.write_bytes(b"not a zip at all")
    assert bookmeta.epub_meta(str(p)) is None


def test_epub_meta_missing_container_returns_none(tmp_path):
    p = tmp_path / "empty.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
    assert bookmeta.epub_meta(str(p)) is None


def test_epub_meta_nonexistent_file_returns_none(tmp_path):
    assert bookmeta.epub_meta(str(tmp_path / "nope.epub")) is None


# ── mobi_meta ────────────────────────────────────────────────────────────────

def _write_mobi(path, db_name=b"MyBook", author="Frank Herbert",
                title="Dune", include_exth=True):
    """A minimal synthetic PalmDB/MOBI blob: 78-byte header (name, type/creator
    at offset 60, record count at 76) + a record-0 offset entry + a record 0
    carrying an EXTH block with authors (100) and updated-title (503)."""
    header = bytearray(78)
    name = db_name[:32].ljust(32, b"\x00")
    header[0:32] = name
    header[60:68] = b"BOOKMOBI"
    struct.pack_into(">H", header, 76, 1)          # num_records = 1
    rec0_offset = 78 + 8                            # header + 1 record-info entry
    record_info = struct.pack(">II", rec0_offset, 0)

    exth = b""
    if include_exth:
        items = b""
        for rec_type, val in ((100, author.encode()), (503, title.encode())):
            items += struct.pack(">II", rec_type, 8 + len(val)) + val
        exth_body = struct.pack(">4sII", b"EXTH", 12 + len(items), 2) + items
        exth = exth_body
    rec0 = b"\x00" * 16 + exth
    data = bytes(header) + record_info + rec0
    with open(path, "wb") as f:
        f.write(data)


def test_mobi_meta_extracts_author_and_title(tmp_path):
    p = tmp_path / "book.mobi"
    _write_mobi(p)
    m = bookmeta.mobi_meta(str(p))
    assert m["author"] == "Frank Herbert"
    assert m["title"] == "Dune"


def test_mobi_meta_falls_back_to_db_name_when_no_exth_title(tmp_path):
    p = tmp_path / "book.mobi"
    _write_mobi(p, db_name=b"Dune_Herbert", include_exth=False)
    m = bookmeta.mobi_meta(str(p))
    assert m["title"] == "Dune_Herbert"
    assert m["author"] is None


def test_mobi_meta_not_a_palmdb_returns_none(tmp_path):
    p = tmp_path / "junk.mobi"
    p.write_bytes(b"garbage" * 20)
    assert bookmeta.mobi_meta(str(p)) is None


def test_mobi_meta_nonexistent_file_returns_none(tmp_path):
    assert bookmeta.mobi_meta(str(tmp_path / "nope.mobi")) is None


# ── parse_name: measured live shapes ─────────────────────────────────────────

def test_parse_name_author_dash_title():
    r = bookmeta.parse_name("Douglas Adams - The Hitchhiker's Guide to the Galaxy")
    assert r == {"author": "Douglas Adams",
                "title": "The Hitchhiker's Guide to the Galaxy",
                "series": None, "series_index": None}


def test_parse_name_comma_author_series_nn_title():
    r = bookmeta.parse_name(
        "Adams, Douglas - HGG 1 - Life, the Universe and Everything")
    assert r["author"] == "Adams, Douglas"
    assert r["series"] == "HGG"
    assert r["series_index"] == 1
    assert r["title"] == "Life, the Universe and Everything"


def test_parse_name_comma_author_no_series():
    r = bookmeta.parse_name("Adams, Douglas - Life, the Universe and Everything")
    assert r["author"] == "Adams, Douglas"
    assert r["series"] is None
    assert r["title"] == "Life, the Universe and Everything"


def test_parse_name_nn_author_series_nn_title():
    r = bookmeta.parse_name(
        "01 - Douglas Adams - Hitchhiker's Guide 1 - The Hitchhiker's Guide")
    assert r["author"] == "Douglas Adams"
    assert r["series"] == "Hitchhiker's Guide"
    assert r["series_index"] == 1
    assert r["title"] == "The Hitchhiker's Guide"


def test_parse_name_authorless_nn_series_nn_title():
    r = bookmeta.parse_name("01 - Discworld 05 - Wyrd Sisters")
    assert r["author"] is None
    assert r["series"] == "Discworld"
    assert r["series_index"] == 5
    assert r["title"] == "Wyrd Sisters"


def test_parse_name_camelcase_title_only():
    r = bookmeta.parse_name("AdventuresOfHuckleberryFinn")
    assert r["author"] is None
    assert r["series"] is None
    assert r["title"] == "Adventures Of Huckleberry Finn"


def test_parse_name_strips_rip_tags():
    r = bookmeta.parse_name("Douglas Adams - The Hitchhiker's Guide (v5.0)")
    assert r["author"] == "Douglas Adams"
    assert r["title"] == "The Hitchhiker's Guide"
    r2 = bookmeta.parse_name("Frank Herbert - Dune [retail]")
    assert r2["title"] == "Dune"


def test_parse_name_never_raises_on_empty():
    assert bookmeta.parse_name("") == {
        "author": None, "title": None, "series": None, "series_index": None}


def test_parse_name_never_raises_on_dash_only():
    r = bookmeta.parse_name(" - ")
    assert r["title"] is None or isinstance(r["title"], str)


# ── shelf_author ─────────────────────────────────────────────────────────────

def test_shelf_author_flips_natural_form():
    assert bookmeta.shelf_author("Frank Herbert") == "Herbert, Frank"


def test_shelf_author_keeps_already_comma_form():
    assert bookmeta.shelf_author("Herbert, Frank") == "Herbert, Frank"


def test_shelf_author_particle_le_guin():
    assert bookmeta.shelf_author("Ursula K. Le Guin") == "Le Guin, Ursula K."


def test_shelf_author_particle_van_vogt():
    assert bookmeta.shelf_author("A. E. van Vogt") == "van Vogt, A.E."


def test_shelf_author_particle_de_la_cruz():
    assert bookmeta.shelf_author("Melissa de la Cruz") == "de la Cruz, Melissa"


def test_shelf_author_single_name_kept():
    assert bookmeta.shelf_author("Homer") == "Homer"


def test_shelf_author_suffix_after_surname():
    assert bookmeta.shelf_author("Martin Luther King Jr.") == \
        "King Jr., Martin Luther"


def test_shelf_author_initials_normalize_equal():
    a = bookmeta.shelf_author("J. R. R. Tolkien")
    b = bookmeta.shelf_author("J.R.R. Tolkien")
    assert a == b == "Tolkien, J.R.R."
    assert a is not None


def test_shelf_author_rejects_junk():
    for junk in ("Unknown", "Administrator", "Anonymous", "N/A", "Xy"):
        assert bookmeta.shelf_author(junk) is None


def test_shelf_author_none_input():
    assert bookmeta.shelf_author(None) is None


def test_shelf_author_never_raises_on_weird_input():
    for s in ("", "   ", ",", "- - -", "a,b,c"):
        bookmeta.shelf_author(s)   # must not raise


# ── safe_segment ─────────────────────────────────────────────────────────────

def test_safe_segment_strips_illegal_chars():
    assert bookmeta.safe_segment(
        "The Wise Man's Fear: The Kingkiller Chronicle") == \
        "The Wise Man's Fear The Kingkiller Chronicle"


def test_safe_segment_strips_trailing_dots_and_spaces():
    assert bookmeta.safe_segment("Title.  ") == "Title"


def test_safe_segment_caps_length():
    s = bookmeta.safe_segment("x" * 300)
    assert len(s) <= 120


def test_safe_segment_empty_never_raises():
    assert bookmeta.safe_segment("") == "_"
    assert bookmeta.safe_segment("   ") == "_"
    assert bookmeta.safe_segment("...") == "_"


# ── identity() precedence ─────────────────────────────────────────────────────

def test_identity_epub_embedded_beats_filename(tmp_path):
    p = tmp_path / "some junk name.epub"
    _write_epub(p, title="Dune", author="Frank Herbert")
    r = bookmeta.identity(str(p), os.path.basename(str(p)))
    assert r["author"] == "Frank Herbert"
    assert r["title"] == "Dune"


def test_identity_falls_back_to_parse_name_when_epub_unreadable(tmp_path):
    p = tmp_path / "Frank Herbert - Dune.epub"
    p.write_bytes(b"not a zip")
    r = bookmeta.identity(str(p), os.path.basename(str(p)))
    assert r["author"] == "Frank Herbert"
    assert r["title"] == "Dune"


def test_identity_lit_has_no_reader_uses_parse_name(tmp_path):
    p = tmp_path / "Adams, Douglas - HGG 1 - Life.lit"
    p.write_bytes(b"whatever bytes a dead MS format has")
    r = bookmeta.identity(str(p), os.path.basename(str(p)))
    assert r["author"] == "Adams, Douglas"
    assert r["series"] == "HGG"


def test_identity_mobi_embedded(tmp_path):
    p = tmp_path / "junk123.mobi"
    _write_mobi(p, author="Frank Herbert", title="Dune")
    r = bookmeta.identity(str(p), os.path.basename(str(p)))
    assert r["author"] == "Frank Herbert"
    assert r["title"] == "Dune"


def test_identity_never_raises_on_missing_file(tmp_path):
    r = bookmeta.identity(str(tmp_path / "nope.epub"), "nope.epub")
    assert isinstance(r, dict)


# ── hypothesis property tests (v2 lesson: parsers get property tests) ───────

text_st = st.text(min_size=0, max_size=60)


@given(text_st)
def test_parse_name_never_raises(stem):
    r = bookmeta.parse_name(stem)
    assert set(r) == {"author", "title", "series", "series_index"}


@given(text_st)
def test_shelf_author_never_raises(name):
    bookmeta.shelf_author(name)   # must not raise for any input


@given(text_st)
def test_safe_segment_never_raises_and_has_no_illegal_chars(s):
    out = bookmeta.safe_segment(s)
    assert isinstance(out, str) and out
    assert not bookmeta._ILLEGAL_RE.search(out)
    assert out == out.rstrip(" .")


@given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)),
              min_size=1, max_size=60))
def test_safe_segment_length_capped(s):
    assert len(bookmeta.safe_segment(s, max_len=50)) <= 50


def test_parse_name_bracketed_series_extracts_and_cleans_title():
    """Tech-lead review finding: '[Series NN]' rip convention (measured live)
    must become series/index, never leak into the title — .lit/.rtf have no
    embedded metadata to rescue them."""
    p = bookmeta.parse_name(
        "Adrian Tchaikovsky - [Shadows of the Apt 03] - "
        "Blood of the Mantis (v5.0) (epub)")
    assert p["author"] == "Adrian Tchaikovsky"
    assert p["series"] == "Shadows of the Apt"
    assert p["series_index"] == 3
    assert p["title"] == "Blood of the Mantis"


def test_parse_name_bracketed_series_at_start_and_no_index():
    p = bookmeta.parse_name("[Discworld] - Guards! Guards!")
    assert p["series"] == "Discworld"
    assert p["series_index"] is None
    assert p["title"] == "Guards! Guards!"


def test_parse_name_pure_number_bracket_is_not_a_series():
    p = bookmeta.parse_name("Some Title [2004]")
    assert p["series"] is None


# ── C44 guard 1: embedded-author plausibility (identity) ─────────────────────

def test_identity_rejects_title_flip_embedded_author_falls_back_to_filename(
        tmp_path):
    p = (tmp_path /
         "01 - Michelle West - Sun Sword 01 - The Broken Crown.mobi")
    _write_mobi(p, author="sword, sun", title="The Broken Crown")
    r = bookmeta.identity(str(p), os.path.basename(str(p)))
    assert bookmeta.shelf_author(r["author"]) == "West, Michelle"


def test_identity_rejects_title_flip_with_no_filename_author_yields_none(
        tmp_path):
    p = tmp_path / "Sun Sword.mobi"
    _write_mobi(p, author="sword, sun", title="Sun Sword")
    r = bookmeta.identity(str(p), os.path.basename(str(p)))
    assert r["author"] is None


def test_identity_keeps_plausible_embedded_author(tmp_path):
    p = tmp_path / "junk123.mobi"
    _write_mobi(p, author="Frank Herbert", title="Dune")
    r = bookmeta.identity(str(p), os.path.basename(str(p)))
    assert r["author"] == "Frank Herbert"


# ── C44 guard 2: shelf_author shape sanity ────────────────────────────────────

def test_shelf_author_rejects_article_preposition_first_names():
    cases = [
        "Thorns, Prince of", "Ages, Hero of", "Through, A Man Rides",
        "Novel, Graphic", "Dreams, Knife of", "Front, Storm",
        "West, Guardians of the", "Murgos, King of the",
        "Kell, The Seeress of",
    ]
    for c in cases:
        assert bookmeta.shelf_author(c) is None, c


def test_shelf_author_rejects_natural_form_title_flip():
    # 'Prince of Thorns' flipped naturally ('Thorns' last token) puts an
    # article/preposition first-name in field 2 too.
    assert bookmeta.shelf_author("Prince of Thorns") is None


def test_shelf_author_keeps_real_three_part_names():
    assert bookmeta.shelf_author("Le Guin, Ursula K.") == "Le Guin, Ursula K."
    assert bookmeta.shelf_author("Kay, Guy Gavriel") == "Kay, Guy Gavriel"
    assert bookmeta.shelf_author("de Bodard, Aliette") == "de Bodard, Aliette"
    assert bookmeta.shelf_author("Roberts, John Maddox") == \
        "Roberts, John Maddox"
    assert bookmeta.shelf_author("Vance, Jack") == "Vance, Jack"
    assert bookmeta.shelf_author("Butcher, Jim") == "Butcher, Jim"


# ── C44 guard 3: initials canonicalization ────────────────────────────────────

def test_shelf_author_initials_pgv_forms_all_equal():
    a = bookmeta.shelf_author("Wodehouse, P G")
    b = bookmeta.shelf_author("Wodehouse, P.G")
    c = bookmeta.shelf_author("Wodehouse, P.G.")
    assert a == b == c == "Wodehouse, P.G."


def test_shelf_author_initials_particle_still_works():
    assert bookmeta.shelf_author("Le Guin, Ursula K.") == "Le Guin, Ursula K."
