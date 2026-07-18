r"""Ebook identity (P17/C43): embedded metadata first, filename parse as
fallback — the same evidence-precedence posture as docmeta/exif. Stdlib only,
read-only, and TOTAL: a malformed epub/mobi, an unreadable file, or a name
that parses to nothing returns None/empty fields — never raises (docmeta's
"never raises" posture, copied exactly).

`identity(path, filename)` is the one entrypoint the router calls: embedded
metadata (epub OPF / mobi EXTH) beats a filename parse, which beats nothing.
`parse_name`, `shelf_author` and `safe_segment` are PURE (no I/O) so they get
hypothesis property tests alongside their unit tests (v2 lesson: parsers get
property tests).
"""
from __future__ import annotations

import os
import re
import struct
import zipfile

from . import winpath

MOBI_EXTS = frozenset({".mobi", ".azw", ".azw3", ".azw4", ".prc"})

# ── embedded metadata readers ────────────────────────────────────────────────


def _tag(xml: str, local: str) -> str | None:
    """First <…:local ...>text</…:local> value, tags stripped, or None.
    Tolerant of namespace prefixes and malformed XML (docmeta._tag style)."""
    m = re.search(rf"<(?:\w+:)?{local}\b[^>]*>(.*?)</(?:\w+:)?{local}\s*>",
                  xml, re.S | re.I)
    if not m:
        return None
    val = re.sub(r"<[^>]+>", " ", m.group(1)).strip()
    return val[:300] or None


def _attr(xml: str, local: str, attr: str) -> str | None:
    """First <…:local ... attr="value" .../> attribute value, or None."""
    m = re.search(rf'<(?:\w+:)?{local}\b[^>]*\b{attr}="([^"]*)"', xml, re.I)
    return m.group(1).strip() or None if m else None


def epub_meta(path: str) -> dict | None:
    """Embedded epub identity via META-INF/container.xml -> the OPF ->
    dc:title/dc:creator/dc:language + calibre:series/series_index. None for
    anything unreadable, not a zip, or missing the required parts."""
    try:
        with zipfile.ZipFile(winpath.to_long(path)) as z:
            names = set(z.namelist())
            if "META-INF/container.xml" not in names:
                return None
            container = z.read("META-INF/container.xml").decode(
                "utf-8", "ignore")
            opf_path = _attr(container, "rootfile", "full-path")
            if not opf_path or opf_path not in names:
                return None
            opf = z.read(opf_path).decode("utf-8", "ignore")
    except (zipfile.BadZipFile, OSError, KeyError, RuntimeError,
            NotImplementedError):
        return None
    title = _tag(opf, "title")
    creator = _tag(opf, "creator")
    language = _tag(opf, "language")
    series = series_index = None
    m = re.search(
        r'<meta[^>]+name="calibre:series"[^>]+content="([^"]*)"', opf, re.I)
    if m:
        series = m.group(1).strip() or None
    m = re.search(
        r'<meta[^>]+name="calibre:series_index"[^>]+content="([^"]*)"',
        opf, re.I)
    if m:
        try:
            series_index = int(float(m.group(1)))
        except (ValueError, OverflowError):     # "inf" must not escape (TOTAL)
            series_index = None
    if not title and not creator:
        return None
    return {"author": creator, "title": title, "language": language,
            "series": series, "series_index": series_index}


def mobi_meta(path: str) -> dict | None:
    """Embedded mobi/azw identity: PalmDB header ('BOOKMOBI') + EXTH records
    100 (author) and 503 (updated title); falls back to the PalmDB database
    name. None for anything unreadable or not a recognizable PalmDB/MOBI
    file."""
    try:
        with open(winpath.to_long(path), "rb") as f:
            head = f.read(78)
            if len(head) < 78:
                return None
            db_name = head[:32].split(b"\x00", 1)[0].decode(
                "latin-1", "ignore").strip()
            if head[60:68] not in (b"BOOKMOBI", b"TEXtREAd"):
                return None
            f.seek(76)
            num_records = struct.unpack(">H", f.read(2))[0]
            if num_records < 1:
                return None
            f.seek(78)
            rec0_offset = struct.unpack(">I", f.read(4))[0]
            f.seek(rec0_offset)
            rec0 = f.read(4096)
    except (OSError, struct.error, IndexError):
        return None
    author = title = None
    exth_off = rec0.find(b"EXTH")
    if exth_off != -1:
        try:
            n_items = struct.unpack(">I", rec0[exth_off + 8:exth_off + 12])[0]
            pos = exth_off + 12
            for _ in range(n_items):
                if pos + 8 > len(rec0):
                    break
                rec_type, rec_len = struct.unpack(
                    ">II", rec0[pos:pos + 8])
                val = rec0[pos + 8:pos + rec_len].decode("utf-8", "ignore")
                if rec_type == 100 and not author:
                    author = val.strip() or None
                elif rec_type == 503 and not title:
                    title = val.strip() or None
                pos += rec_len
        except (struct.error, IndexError):
            pass
    if not title:
        title = db_name or None
    if not author and not title:
        return None
    return {"author": author, "title": title, "language": None,
            "series": None, "series_index": None}


# ── filename parsing (pure) ──────────────────────────────────────────────────

_RIP_TAG_RE = re.compile(
    r"\s*[\(\[](?:v?\d+(?:\.\d+)?|epub|mobi|retail|azw3?|converted|"
    r"[a-z0-9]+ release)[\)\]]\s*", re.IGNORECASE)
# '[Series Name 03]' — the bracketed-series rip convention, measured live on
# the epub corpus ('Adrian Tchaikovsky - [Shadows of the Apt 03] - Blood of
# the Mantis'). Extracted BEFORE the dash-split so the bracket never leaks
# into a title on metadata-less formats.
_BRACKET_SERIES_RE = re.compile(
    r"\s*\[\s*([^\]\d][^\]]*?)\s*(\d{1,3})?\s*\]\s*(?:-\s*)?")
_NN_PREFIX_RE = re.compile(r"^\s*(\d{1,3})\s*-\s*")
_COMMA_AUTHOR_RE = re.compile(
    r"^([A-Za-z][A-Za-z.\'-]*,\s*[A-Za-z][A-Za-z .\'-]*?)\s*-\s*(.+)$")
_SERIES_NN_TAIL_RE = re.compile(r"^(.*?)\s+(\d{1,3})\s*-\s*(.+)$")
_SERIES_NN_WHOLE_RE = re.compile(r"^(.*?)\s+(\d{1,3})$")
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Za-z])(?=\d)")


def _strip_rip_tags(stem: str) -> str:
    return _RIP_TAG_RE.sub(" ", stem).strip(" -_")


def _split_series_nn_tail(tail: str) -> tuple[str | None, int | None, str]:
    """'Series NN - Title' -> (series, index, title); no match -> (None, None, tail)."""
    m = _SERIES_NN_TAIL_RE.match(tail)
    if m:
        return m.group(1).strip() or None, int(m.group(2)), m.group(3).strip()
    return None, None, tail


def _camel_split(stem: str) -> str:
    if " " in stem or "_" in stem:
        return stem.replace("_", " ")
    parts = _CAMEL_SPLIT_RE.split(stem)
    return " ".join(p for p in parts if p) if len(parts) > 1 else stem


def parse_name(stem: str) -> dict:
    """PURE filename parser for the measured shapes (never raises). Returns
    {author, title, series, series_index} — any value may be None. Order:
    strip rip-tags -> comma-author ('Last, First - Series NN - Title') ->
    'NN - Author - Series NN - Title' (>=3 ' - '-delimited parts) ->
    authorless 'NN - Series NN - Title' (2 parts, catalog-prefixed) ->
    'Author - Title' (2 parts, no catalog prefix) -> CamelCase title split
    (last resort, no delimiter at all)."""
    out = {"author": None, "title": None, "series": None, "series_index": None}
    try:
        s = _strip_rip_tags(stem)
        if not s:
            return out

        # Bracketed series first: '[Shadows of the Apt 03]' anywhere in the
        # stem names the series (+ optional index) and is removed before the
        # dash-split — epubs are rescued by embedded OPF anyway, but .lit/.rtf
        # have no metadata to fall back on.
        bm = _BRACKET_SERIES_RE.search(s)
        if bm:
            out["series"] = bm.group(1).strip() or None
            if bm.group(2):
                out["series_index"] = int(bm.group(2))
            s = (s[:bm.start()] + " - " + s[bm.end():]).strip(" -_")
            s = re.sub(r"\s+-\s+-\s+", " - ", s)

        # 'NN - ...' catalog-index prefix strip first (not part of author or
        # series — a plain numeric ordering tag).
        m = _NN_PREFIX_RE.match(s)
        had_nn = bool(m)
        rest = s[m.end():].strip() if m else s
        if not rest:
            return out

        # Comma-author: 'Last, First - Series NN - Title' or 'Last, First - Title'
        cm = _COMMA_AUTHOR_RE.match(rest)
        if cm:
            out["author"] = cm.group(1).strip()
            series, idx, title = _split_series_nn_tail(cm.group(2).strip())
            out["series"], out["series_index"], out["title"] = \
                series, idx, title or None
            return out

        parts = [p for p in rest.split(" - ")]
        if len(parts) >= 3:
            # 'Author - Series NN - Title' (author unambiguous — it's the
            # first of >=2 dashes, the series-number split applies to the tail
            # only, never swallowing the author).
            author_cand = parts[0].strip()
            tail = " - ".join(parts[1:]).strip()
            series, idx, title = _split_series_nn_tail(tail)
            out["author"] = author_cand or None
            if series or idx is not None:
                out["series"], out["series_index"], out["title"] = \
                    series, idx, title or None
            else:
                out["title"] = tail or None
            return out

        if len(parts) == 2:
            a, b = parts[0].strip(), parts[1].strip()
            wm = _SERIES_NN_WHOLE_RE.match(a) if had_nn else None
            if wm:
                # authorless 'NN - Series NN - Title' (catalog prefix already
                # stripped; 'a' is itself 'Series NN')
                out["series"] = wm.group(1).strip() or None
                out["series_index"] = int(wm.group(2))
                out["title"] = b or None
                return out
            out["author"] = a or None
            out["title"] = b or None
            return out

        # Last resort: CamelCase / no-delimiter title
        out["title"] = _camel_split(rest) or rest
        return out
    except Exception:
        return {"author": None, "title": None, "series": None,
                "series_index": None}


# ── author-name shelving (pure) ──────────────────────────────────────────────

_PARTICLES = frozenset({
    "le", "la", "van", "von", "de", "del", "della", "di", "da", "du",
    "der", "den", "ter", "ten", "mac", "mc", "st", "saint", "bin", "ibn",
})
_SUFFIXES = frozenset({"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"})
_JUNK_AUTHORS = frozenset({
    "unknown", "administrator", "anonymous", "n/a", "na", "author",
    "calibre", "converter", "unspecified",
})

# C44 guard 2 — a real "Last, First" has a plausible given-name in field 2.
# Articles/prepositions are the load-bearing half; title nouns are a
# belt-and-braces list seeded from the owner-found flip cases (spec
# the C44 design spec).
_SHAPE_STOPWORDS = frozenset({
    "a", "an", "the", "of", "in", "on", "to", "for", "and", "or", "from",
    "with", "at", "by", "new", "prince", "king", "queen", "lord", "hero",
    "knife", "storm", "moon", "blood", "death", "graphic", "novel", "court",
    "crown", "shadow", "road", "way", "sword", "sun", "rites", "favor",
    "guilty", "coat", "peril", "masks", "night", "spring", "twilight",
    "ages", "dust", "daughter", "seeress", "sorceress", "guardians",
    "dreams", "uncrowned", "shining", "steerswoman",
})


def _implausible_given_name(first: str) -> bool:
    """C44 guard 2: reject a 'Last, First' flip whose given-name field opens
    with an article/preposition/title-noun (Prince of Thorns, A Man Rides
    Through) — the tell that a title/series string got flipped as a name."""
    tokens = first.split()
    return bool(tokens) and tokens[0].strip(".,").lower() in _SHAPE_STOPWORDS


_SINGLE_INITIAL_RE = re.compile(r"^[A-Za-z]\.?$")
_DOTTED_INITIAL_RUN_RE = re.compile(r"^(?:[A-Za-z]\.){1,}[A-Za-z]?\.?$")


def _normalize_initials(name: str) -> str:
    """C44 guard 3: collapse any run of single-letter initials — space- or
    dot-separated, as whitespace tokens ('P G', 'J. R. R.') or fused into one
    token ('P.G', 'J.R.R.') — into dotted-no-spaces form ('P.G.', 'J.R.R.').
    A lone initial next to a real word ('R. Tolkien') is left untouched; a
    real multi-letter word never matches (it must already contain a '.' to be
    treated as fused initials)."""
    tokens = name.split(" ")
    out: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if "." in t and _DOTTED_INITIAL_RUN_RE.match(t):
            letters = re.findall(r"[A-Za-z]", t)
            if len(letters) >= 2:
                out.append("".join(c.upper() + "." for c in letters))
                i += 1
                continue
        if _SINGLE_INITIAL_RE.match(t):
            letters = [t[0].upper()]
            j = i + 1
            while j < len(tokens) and _SINGLE_INITIAL_RE.match(tokens[j]):
                letters.append(tokens[j][0].upper())
                j += 1
            if len(letters) >= 2:
                out.append("".join(c + "." for c in letters))
            else:
                out.append(t)
            i = j
            continue
        out.append(t)
        i += 1
    return " ".join(out)


def shelf_author(name: str | None) -> str | None:
    """Normalize an author name to 'Last, First' for shelving. Already-comma
    forms are kept verbatim (whitespace-normalized); a natural 'First [Middle]
    Last' form is flipped on the last token, unless the penultimate token is a
    surname particle (Le Guin, van Vogt, de la Cruz — never split mid-surname);
    suffixes (Jr/Sr/II/III) attach after the surname. Junk (Unknown,
    Administrator, converter noise, <3 chars) -> None. Never raises."""
    if not name:
        return None
    n = _normalize_initials(re.sub(r"\s+", " ", name.strip()))
    if len(n) < 3 or n.lower() in _JUNK_AUTHORS:
        return None

    if "," in n:
        last, _, first = n.partition(",")
        last, first = last.strip(), first.strip()
        if not last or not first or _implausible_given_name(first):
            return None
        return f"{last}, {first}"

    tokens = n.split(" ")
    if len(tokens) < 2:
        return None if len(n) < 3 else n   # a single-name author: keep as-is

    suffix = None
    if tokens[-1].strip(".").lower() in _SUFFIXES:
        suffix = tokens[-1]
        tokens = tokens[:-1]
    if len(tokens) < 2:
        return None

    # Surname particle(s): walk backward from the (pre-suffix) last token,
    # absorbing any run of particle tokens into the surname — 'Ursula K. Le
    # Guin' -> surname 'Le Guin', never 'Guin' with 'Le' left in the first name.
    split_at = len(tokens) - 1
    while split_at > 0 and tokens[split_at - 1].lower() in _PARTICLES:
        split_at -= 1
    surname = " ".join(tokens[split_at:])
    first = " ".join(tokens[:split_at])
    if not first or _implausible_given_name(first):
        return None
    if suffix:
        surname = f"{surname} {suffix}"
    return f"{surname}, {first}"


_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_segment(s: str, max_len: int = 120) -> str:
    """Windows-safe path segment: strip illegal chars/control codes, collapse
    whitespace, drop trailing dots/spaces, cap length. Never raises."""
    if not s:
        return "_"
    out = _ILLEGAL_RE.sub(" ", s)
    out = re.sub(r"\s+", " ", out).strip()
    out = out.rstrip(" .")
    out = out[:max_len].rstrip(" .")
    return out or "_"


# ── identity (embedded-first, filename fallback) ─────────────────────────────

_TOKENIZE_RE = re.compile(r"[a-z0-9]+")
_GRAMMAR_STOPWORDS = frozenset({
    "a", "an", "the", "of", "in", "on", "to", "for", "and", "or", "from",
    "with", "at", "by", "new",
})


def _tokenize(s: str | None) -> set[str]:
    if not s:
        return set()
    return {w for w in _TOKENIZE_RE.findall(s.lower())
            if w not in _GRAMMAR_STOPWORDS}


def _embedded_author_is_title_flip(author: str, title: str | None,
                                    series: str | None) -> bool:
    """C44 guard 1: reject an embedded author string that is actually the
    book's own title/series text (mobi EXTH 'author' carrying TITLE/SERIES/
    format junk) — >=50% of its tokens overlap the title+series tokens."""
    author_tokens = _tokenize(author)
    if not author_tokens:
        return False
    other_tokens = _tokenize(title) | _tokenize(series)
    overlap = len(author_tokens & other_tokens) / len(author_tokens)
    return overlap >= 0.5


def identity(path: str, filename: str) -> dict:
    """Book identity for one file: embedded metadata (by extension) beats a
    filename parse. Returns {author, title, series, series_index} (any may be
    None). Never raises — a malformed reader falls through to parse_name.

    C44 guard 1: an embedded author that looks like the book's own
    title/series (a mobi EXTH field carrying junk, not a name) is rejected in
    favor of the filename-parsed author; if neither is plausible, author is
    left None rather than shelving under the title."""
    ext = os.path.splitext(filename)[1].lower()
    embedded = None
    if ext == ".epub":
        embedded = epub_meta(path)
    elif ext in MOBI_EXTS:
        embedded = mobi_meta(path)
    # .lit/.fb2/.djvu (and any format the readers refused): no embedded reader,
    # or the reader found nothing — fall through to parse_name.
    stem = os.path.splitext(filename)[0]
    parsed = parse_name(stem)
    if embedded:
        embedded_author = embedded.get("author")
        title = embedded.get("title") or parsed["title"]
        series = embedded.get("series") or parsed["series"]
        if embedded_author and _embedded_author_is_title_flip(
                embedded_author, title, series):
            embedded_author = None
        return {
            "author": embedded_author or parsed["author"],
            "title": title,
            "series": series,
            "series_index": embedded.get("series_index")
                if embedded.get("series_index") is not None
                else parsed["series_index"],
        }
    return parsed
