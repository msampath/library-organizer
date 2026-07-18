r"""Embedded document metadata — the 'open the file and read its properties'
signal for organizing documents by purpose.

`props(path) -> dict` returns any of {creator, title, subject, keywords,
last_modified_by, company, created, modified} for an Office OOXML file
(.docx/.xlsx/.pptx and their macro variants — all ZIP containers). Stdlib only
(zipfile + a tolerant tag scan), read-only, and TOTAL: a non-OOXML file, a
legacy OLE .doc/.xls/.ppt, or an unreadable/corrupt file returns {} — never
raises (exactly like exif.year_of and sniff.kind_of).

The filename hides purpose ('LVC.pptx' is really a sports quiz; 'Presentation
.pptx' is a research paper); the author/company/internal-title a human would
read after opening it does not. This exposes that so the organizer can think
like a human, not just pattern-match a name.
"""
from __future__ import annotations

import re
import zipfile

from . import winpath

# Extensions worth opening for embedded properties. Callers gate on this so a
# multi-GB video is never slurped by the OLE fallback (which reads whole files).
DOC_EXTS = frozenset({
    ".doc", ".docx", ".docm", ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx",
    ".pptm", ".pps", ".ppsx", ".odt", ".ods", ".odp", ".key", ".vsd", ".msg",
})

# docProps part -> {output key: XML local tag name}. Namespace prefixes vary
# (dc:, cp:, dcterms:), so we match by local name only.
_FIELDS = {
    "docProps/core.xml": {
        "creator": "creator", "title": "title", "subject": "subject",
        "keywords": "keywords", "last_modified_by": "lastModifiedBy",
        "created": "created", "modified": "modified",
    },
    "docProps/app.xml": {"company": "Company", "manager": "Manager"},
}


def _tag(xml: str, local: str) -> str | None:
    """First <…:local>text</…:local> value, tags stripped, or None. Tolerant of
    namespace prefixes and malformed XML — a regex, so a broken part is 'no
    value', never an exception."""
    m = re.search(rf"<(?:\w+:)?{local}\b[^>]*>(.*?)</(?:\w+:)?{local}\s*>",
                  xml, re.S | re.I)
    if not m:
        return None
    val = re.sub(r"<[^>]+>", " ", m.group(1)).strip()
    return val[:200] or None


def props(path: str) -> dict[str, str]:
    """Embedded document properties, or {} for anything else. Reads modern OOXML
    (.docx/.xlsx/.pptx, a ZIP) directly; falls back to the legacy OLE reader for
    binary .doc/.xls/.ppt. Never raises."""
    out: dict[str, str] = {}
    try:
        with zipfile.ZipFile(winpath.to_long(path)) as z:
            names = set(z.namelist())
            for part, fields in _FIELDS.items():
                if part not in names:
                    continue
                try:
                    xml = z.read(part).decode("utf-8", "ignore")
                except (KeyError, OSError, RuntimeError,
                        zipfile.BadZipFile, NotImplementedError):
                    # RuntimeError: encrypted member; NotImplementedError:
                    # unsupported compression — 'never raises' means never.
                    continue
                for key, local in fields.items():
                    v = _tag(xml, local)
                    if v:
                        out[key] = v
        return out
    except zipfile.BadZipFile:
        pass                                # not OOXML — try legacy OLE below
    except OSError:
        return {}
    from . import ole
    return ole.read(path)
