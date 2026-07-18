"""docmeta.props — embedded Office (OOXML) document metadata, total by
property: real props are read, a non-OOXML or corrupt file is honestly {}."""
from __future__ import annotations

import zipfile

from mlo.docmeta import props

CORE = (
    '<?xml version="1.0"?>'
    '<cp:coreProperties xmlns:cp="x" xmlns:dc="y" xmlns:dcterms="z">'
    '<dc:creator>Nina Godles</dc:creator>'
    '<dc:title>CHPW: QDW User stories</dc:title>'
    '<cp:lastModifiedBy>Example Author</cp:lastModifiedBy>'
    '<dcterms:created>2013-10-04T12:00:00Z</dcterms:created>'
    '</cp:coreProperties>'
)
APP = ('<Properties xmlns="x"><Company>Arcadia</Company>'
       '<Application>Microsoft Excel</Application></Properties>')


def _ooxml(tmp_path, name="deck.pptx", core=CORE, app=APP):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        if core is not None:
            z.writestr("docProps/core.xml", core)
        if app is not None:
            z.writestr("docProps/app.xml", app)
    return str(p)


def test_extracts_core_and_app_props(tmp_path):
    m = props(_ooxml(tmp_path))
    assert m["creator"] == "Nina Godles"
    assert m["title"] == "CHPW: QDW User stories"
    assert m["last_modified_by"] == "Example Author"
    assert m["created"].startswith("2013-10-04")
    assert m["company"] == "Arcadia"


def test_namespace_prefixes_are_tolerated(tmp_path):
    # a different prefix for the same local name still resolves
    core = CORE.replace("dc:creator", "foo:creator")
    assert props(_ooxml(tmp_path, core=core))["creator"] == "Nina Godles"


def test_missing_parts_and_empty_values(tmp_path):
    m = props(_ooxml(tmp_path, app=None, core="<cp:coreProperties/>"))
    assert m == {}                       # no fields present -> {}


def test_non_ooxml_and_corrupt_return_empty(tmp_path):
    plain = tmp_path / "notes.txt"
    plain.write_text("just text, not a zip")
    assert props(str(plain)) == {}
    assert props(str(tmp_path / "does-not-exist.docx")) == {}


def test_legacy_ole_binary_returns_empty(tmp_path):
    # a legacy .xls (OLE compound) is not a zip -> {} (never raises)
    ole = tmp_path / "old.xls"
    ole.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    assert props(str(ole)) == {}
