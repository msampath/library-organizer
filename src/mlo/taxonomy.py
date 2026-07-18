"""Pure classifiers, the hierarchical router, and coverage accounting (L4).

Classifiers are total functions: they return a match with rule provenance or
None — and None means UNMATCHED, which is counted and surfaced, never silently
binned. There is no implicit 'Other' bucket anywhere in mlo: routing to
layout.default_language is explicit config data with its own rule id.

route() is the v0.2 organizer brain: content-derived, Jellyfin-compatible
destinations (Movies/Title (Year) under language, Series/Season for TV, year
folders for photos) instead of v0.1's provenance-flat placement. It is PURE —
EXIF years and agent classifications arrive as arguments (Hints), never as I/O
from here — and IDEMPOTENT: a correctly-placed file routes to its own current
path, which is what makes reorganize plans converge to zero rows.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from . import audioclass, bookmeta, containers, imgclass, naming, pathmeta
# Canonical homes moved to containers.py (C33) so taxonomy, config and plan can
# all consume them without an import cycle; re-exported here because every
# existing consumer says taxonomy.MEDIA_LABELS / taxonomy._PROVENANCE_SEG.
from .config import Config
from .containers import MEDIA_LABELS  # noqa: F401  (re-export)
from .containers import PROVENANCE_SEG as _PROVENANCE_SEG

_TOKEN_RE = re.compile(r"[^0-9a-z]+")


def classify_junk(cfg: Config, relpath: str, size: int) -> str | None:
    """Junk rule id, or None. Rule order: zero-byte, name, extension."""
    if cfg.junk_zero_byte and size == 0:
        return "junk:zero-byte"
    base = os.path.basename(relpath).lower()
    if base in cfg.junk_names:
        return "junk:name"
    ext = os.path.splitext(base)[1]
    if ext and ext in cfg.junk_extensions:
        return "junk:ext"
    return None


def bucket_for(cfg: Config, relpath: str) -> tuple[str, str] | None:
    """(bucket_label, rule_id) for a configured extension, else None (UNMATCHED)."""
    ext = os.path.splitext(relpath)[1].lower()
    if not ext:
        return None
    for label, exts in cfg.taxonomy.items():
        if ext in exts:
            return label, f"tax:ext:{ext}"
    return None


# ── the hierarchical router (v0.2) ───────────────────────────────────────────

@dataclass(frozen=True)
class Hints:
    """External judgment for one file: agent classifications or EXIF facts.
    Everything optional; route() never guesses a missing field."""
    media_kind: str | None = None      # 'movie' | 'tv' | 'personal' | None
    language: str | None = None
    year: int | None = None            # EXIF DateTimeOriginal year for photos
    content_kind: str | None = None    # magic-byte sniff: 'video'|'audio'|'image'
    # C43 (P17 Ebooks): identity for a book file, from embedded metadata or a
    # critic/subagent judgment — never guessed by the router itself.
    book_author: str | None = None
    book_title: str | None = None
    book_series: str | None = None
    book_index: int | None = None


@dataclass(frozen=True)
class Route:
    dest_relpath: str                  # native separators, relative to library
    rule: str


def _native(*segments: str) -> str:
    parts: list[str] = []
    for seg in segments:
        parts.extend(p for p in seg.replace("\\", "/").split("/") if p)
    return os.sep.join(parts)


def detect_language(cfg: Config, relpath: str) -> tuple[str, str] | None:
    """(Language, rule) from the path, else None.

    Precedence is positional, strongest first — this is what keeps routing
    IDEMPOTENT for correctly-placed files (a filename tag like '.English.Subs'
    must never outrank the Tamil/ folder the file already lives in):
      1. a directory segment that IS a configured language name,
      2. language tokens in directory names (config order),
      3. language tokens in the filename (config order).
    """
    parts = relpath.replace(os.sep, "/").split("/")
    dirs, filename = parts[:-1], parts[-1]

    lang_by_name = {lang.lower(): lang for lang in cfg.layout.languages}
    for seg in dirs:
        hit = lang_by_name.get(seg.lower())
        if hit:
            return hit, f"lang:segment:{seg.lower()}"

    dir_tokens = set(_TOKEN_RE.split("/".join(dirs).lower()))
    dir_tokens.discard("")
    for language, lang_tokens in cfg.layout.languages.items():
        for t in lang_tokens:
            if t in dir_tokens:
                return language, f"lang:token:{t}"

    file_tokens = set(_TOKEN_RE.split(filename.lower()))
    file_tokens.discard("")
    for language, lang_tokens in cfg.layout.languages.items():
        for t in lang_tokens:
            if t in file_tokens:
                return language, f"lang:file-token:{t}"
    return None


def _language_segment(cfg: Config, relpath: str,
                      hints: Hints | None) -> tuple[list[str], str]:
    if not cfg.layout.language_folders:
        return [], "lang:folders-off"
    hit = detect_language(cfg, relpath)
    if hit:
        return [hit[0]], hit[1]
    if hints and hints.language:
        return [hints.language], "lang:hint"
    return [cfg.layout.default_language], "lang:default"


def _ci(s: str) -> str:
    """Comparison form: forward slashes + casefold. Config roots are user-typed
    and disk paths carry disk case; comparing them any other way silently
    re-arms every idempotence bug on Windows (review finding C15)."""
    return s.replace("\\", "/").strip("/").casefold()


def _inside(posix_rel: str, root: str) -> str | None:
    """The remainder of posix_rel under root (ci prefix match), else None."""
    prefix = _ci(root) + "/"
    if _ci(posix_rel).startswith(prefix):
        # slice the ORIGINAL string by the prefix length (same segment count)
        return posix_rel[len(prefix):]
    return None


def _subtype_route(cfg: Config, relpath: str, sub_root: str,
                   filename: str) -> Route:
    """Place a file into a finer-type sub-root (config `layout.subtypes`),
    keeping its immediate parent grouping. Idempotent: a file already under the
    sub-root stays, so reorganize converges."""
    already = relpath.replace(os.sep, "/")
    if _inside(already, sub_root) is not None:
        return Route(relpath, "route:subtype:already-placed")
    parts = already.split("/")
    tail = parts[-2:] if len(parts) >= 3 else [filename]
    return Route(_native(sub_root, *tail), "route:subtype")


def _media_top_segments(cfg: Config) -> set[str]:
    """Casefolded first segment of every media root ({'video','audio','images'}
    for the default layout) — the areas the router treats as existing structure
    and never reshuffles."""
    lay = cfg.layout
    return {r.replace("\\", "/").split("/")[0].casefold()
            for r in (lay.movies_root, lay.tv_root, lay.music_root,
                      lay.photos_root, lay.personal_root)}


def _sniff_holding_pen(cfg: Config, relpath: str, kind: str) -> Route:
    """Route a content-sniffed false-carve into its media type's Unclassified
    holding pen, or leave it put if it already sits in a media area.

    Conservative and idempotent by construction: a file already under any media
    top segment (Video/Audio/Images) is that tree's business and stays; a carve
    stranded elsewhere (Other/, Documents/, …) moves to <MediaTop>/Unclassified,
    keeping its immediate parent grouping. After the move its first segment IS a
    media top, so the next run leaves it put — reorganize converges to zero."""
    lay = cfg.layout
    already = relpath.replace(os.sep, "/")
    if already.split("/")[0].casefold() in _media_top_segments(cfg):
        return Route(relpath, "route:sniff:already-in-media-area")
    root = {"video": lay.movies_root, "audio": lay.music_root,
            "image": lay.photos_root}[kind]
    top = root.replace("\\", "/").split("/")[0]
    parts = already.split("/")
    tail = parts[-2:] if len(parts) >= 3 else [os.path.basename(relpath)]
    return Route(_native(top, "Unclassified", *tail), f"route:sniff:{kind}")


_COMIC_DELIM = re.compile(r"\s-\s|\s-\d|\s#\d|\svol\b", re.IGNORECASE)


def _normalize_comic_series(name: str) -> str:
    """Strip trailing issue/volume numbers from a comic series folder name
    (C35): `Star Wars 01` -> `Star Wars`, `Marvel Illustrated 01 02` ->
    `Marvel Illustrated`, `Star Wars 3D 02` -> `Star Wars 3D`. Parenthetical
    annotations survive: `Star Wars (Marvel)` stays (a different publisher).
    Preserves original when no trailing number is present."""
    n = name
    while True:
        m = re.search(r"\s+\d+$", n)
        if not m:
            break
        n = n[:m.start()]
    return n.strip() or name


def _comic_series(filename: str) -> str | None:
    """Series folder for a comic — the text before the first title/issue
    delimiter: ' - ', a ' -NN-' issue marker, ' #NN', or ' Vol'. Handles both
    'Star Wars - Crimson Empire 01' -> 'Star Wars' and 'Asterix -01- The Gaul
    - 1961' -> 'Asterix'. None -> place flat (no recognizable series)."""
    stem = os.path.splitext(filename)[0]
    parts = _COMIC_DELIM.split(stem, maxsplit=1)
    if len(parts) > 1:
        series = parts[0].strip(" -")
        if len(re.sub(r"[^A-Za-z]", "", series)) >= 2:
            return series
    return None


def _book_dest(ebooks_root: str, shelf_author: str, series: str | None,
              index: int | None, title: str, ext: str) -> str:
    """Books\\<Last, First>\\[<Series>\\]<NN - Title|Title><ext> — every
    segment (embedded titles carry ':' etc.) through the Windows-safe
    sanitizer (bookmeta.safe_segment; no existing routing-path sanitizer,
    integration-report finding)."""
    t = bookmeta.safe_segment(title)
    fname = f"{index:02d} - {t}{ext}" if index is not None else f"{t}{ext}"
    segs = [ebooks_root, bookmeta.safe_segment(shelf_author)]
    if series:
        segs.append(bookmeta.safe_segment(series))
    segs.append(fname)
    return _native(*segs)


def route(cfg: Config, relpath: str, hints: Hints | None = None) -> Route | None:
    """Content-derived destination for a MEDIA file, or None.

    None means either "no bucket at all" (UNMATCHED, coverage-counted) or
    "media identity not derivable / non-media bucket" — in both cases the
    caller decides: organize falls back to provenance-flat placement for
    bucketed files, reorganize leaves the file where it is.

    THE POSTURE (review finding C15, the user's hard constraint): a file
    already under its media root STAYS THERE unless an explicit,
    evidence-backed repair applies. The router organizes the unorganized; it
    never re-derives, flattens, or second-guesses existing library structure —
    artist trees, photo albums, hand-named series folders are all the
    library's business. All root/segment comparisons are case- and
    separator-insensitive."""
    lay = cfg.layout
    # Container members stay put (C33) — checked BEFORE bucket_for because a
    # container holds media and non-media alike, and its files must never be
    # routed individually (splitting a snapshot destroys it). Covers both a
    # container pending relocation (build_containers owns the move) and one
    # already at home (the builder's idempotence yields no rows).
    if containers.root_of(cfg, relpath) is not None:
        return Route(relpath, "route:container:member")
    bucket = bucket_for(cfg, relpath)
    if bucket is None:
        # A configured extension yields no bucket. If a content sniff (magic
        # bytes) says the file IS media, it is a false-carve — a recovery blob
        # in a '.swf'/'.dat' whose extension lies. Route it to that media
        # type's Unclassified holding pen (§2.5) so a critic or the human
        # places it: evidence-backed RECLASSIFICATION, never a guess at the
        # specific home. Content is consulted ONLY here, so it can never
        # override the user's extension taxonomy.
        if hints and hints.content_kind in ("video", "audio", "image"):
            return _sniff_holding_pen(cfg, relpath, hints.content_kind)
        return None
    label, _ = bucket
    filename = os.path.basename(relpath)
    already = relpath.replace(os.sep, "/")
    lang_names = {n.casefold() for n in lay.languages} | {
        lay.default_language.casefold()}

    # Cross-type conservatism (review of the first real repair plan): a file
    # inside ANOTHER media type's root is that tree's business — album art
    # inside Music/, posters inside Movies/, a clip inside a photo album. The
    # type-owning branch below applies its own already-placed/repair rules;
    # everything else under a media root simply stays.
    _owner = {"Video": (lay.tv_root, lay.movies_root, lay.personal_root),
              "Videos": (lay.tv_root, lay.movies_root, lay.personal_root),
              "Audio": (lay.music_root,),
              "Photos": (lay.photos_root,),
              "Images": (lay.photos_root,),
              "Comics": (lay.comics_root,),
              "Ebooks": (lay.ebooks_root,)}
    for root in (lay.music_root, lay.tv_root, lay.movies_root,
                 lay.photos_root, lay.personal_root,
                 lay.comics_root, lay.ebooks_root):
        if _inside(already, root) is not None and \
                root not in _owner.get(label, ()):
            return Route(relpath, "route:sidecar:already-placed")

    # Finer types (§7): a critic-assigned media_kind that names a configured
    # sub-root (layout.subtypes) routes there — WhatsApp, Anime, Ads, Sports,
    # Audiobooks, System_Sounds, Screenshots, Graphics. Disjoint from the
    # movie/tv/personal/music kinds handled below; only for media buckets.
    if hints and hints.media_kind and label in MEDIA_LABELS:
        sub = lay.subtypes.get(hints.media_kind)
        if sub:
            return _subtype_route(cfg, relpath, sub, filename)

    if label in ("Video", "Videos"):
        if _inside(already, lay.personal_root) is not None:
            # Personal is pure human placement — there is no naming convention
            # to repair toward, so nothing under it moves. Not even a
            # movie-parseable filename or an agent hint outranks the human who
            # put a video HERE (same rule as the music root).
            return Route(relpath, "route:personal:already-placed")
        name = naming.parse_media_name(filename)
        kind = hints.media_kind if hints and hints.media_kind else None

        inner_tv = _inside(already, lay.tv_root)
        if inner_tv is not None:
            # Under the TV root: any series-level grouping (>= series/file
            # depth past an optional language dir) is trusted as-is. Only a
            # file lying loose (directly under the root or a language dir)
            # gets foldered into Series/Season NN.
            depth = inner_tv.count("/")
            segs = inner_tv.split("/")
            loose = depth == 0 or (depth == 1 and segs[0].casefold() in lang_names)
            if not (loose and name.is_episode):
                return Route(relpath, "route:tv:already-placed")

        inner_mv = _inside(already, lay.movies_root)
        if inner_mv is not None and inner_mv.count("/") >= 1:
            parent = inner_mv.split("/")[-2]
            parent_year = naming.parse_year(parent)
            if parent_year is not None and (
                    name.year is None or parent_year == name.year) \
                    and not naming.has_year_stutter(parent):
                # inside a Title (Year) folder that doesn't contradict the
                # filename: a valid home, whatever the folder is called.
                # EXCEPTION (C26): year-stuttered parents fall through and get
                # re-routed to the clean 'Title (Year)' — one narrow disease,
                # not general renaming. Junk-tagged parents (no stutter) keep
                # their tolerance and stay put.
                return Route(relpath, "route:movie:already-placed")

        if name.is_episode and kind in (None, "tv"):
            lang, lrule = _language_segment(cfg, relpath, hints)
            series = name.title or "Unknown Series"
            return Route(
                _native(lay.tv_root, *lang, series,
                        naming.season_folder(name.season), filename),
                f"route:tv:{lrule}")
        ext = os.path.splitext(filename)[1]
        folder = naming.movie_folder(name)
        if folder and kind in (None, "movie"):
            # A file moving INTO a movie home gets the Jellyfin clean name,
            # folder and file alike: Title (Year)/Title (Year).ext (user
            # directive — release-scene dots and quality tags don't survive
            # the move). Files already home are guarded above and never
            # renamed. Multi-part rips collide on the clean name and stay put.
            lang, lrule = _language_segment(cfg, relpath, hints)
            return Route(
                _native(lay.movies_root, *lang, folder, folder + ext),
                f"route:movie:{lrule}")
        if kind == "personal":
            # Keep the immediate parent-dir grouping (a dashcam folder, a
            # phone-backup album) — flat Personal/<filename> for a mass of
            # agent-labeled clips would interleave every source into one dir
            # and collide on camera basenames (the C19 shelf-not-home shape).
            parts = already.split("/")
            tail = parts[-2:] if len(parts) >= 3 else [filename]
            return Route(_native(lay.personal_root, *tail), "route:personal:hint")
        if kind == "movie" and hints and hints.year:
            lang, lrule = _language_segment(cfg, relpath, hints)
            title = name.title or os.path.splitext(filename)[0]
            # 'Where Eagles Dare1968': the L3 parser rightly refused the
            # unparenthesized year, but the attested year CONFIRMS it — safe
            # to strip from the title for naming (and only then).
            if title.endswith(str(hints.year)):
                title = title[:-4].rstrip(" .-_(") or title
            folder = f"{title} ({hints.year})"
            return Route(
                _native(lay.movies_root, *lang, folder, folder + ext),
                f"route:movie:hint-year:{lrule}")

        # Last resort: the filename gave no identity, but the FULL PATH may —
        # a DVD rip / provenance-dumped movie whose folder names the type,
        # language and title (Videos\<src>\Movies\Tamil\<Title>\VTS_*.VOB).
        # The original filename is kept (multi-part rips co-locate, no rename).
        pm = pathmeta.derive(cfg, relpath)
        if pm.media_type == "movie" and pm.title:
            lang, lrule = _language_segment(cfg, relpath, hints)
            return Route(_native(lay.movies_root, *lang, pm.title, filename),
                         f"route:movie:path:{lrule}")
        # Still no identity. A video STRANDED in a WRONG media root (top-level
        # Videos\ or Photos\ — a duplicate of the canonical Video\) is drained
        # into the canonical tree: a devotional-DVD path (a bhajan/discourse rip,
        # no Movies segment) names a home (Video\Devotional); anything else goes
        # to the Video\Unsorted shelf (the drain rule moves that only when
        # consolidating a wrong media root). This is scoped to wrong MEDIA roots
        # so organize's source-relative inputs still fall through to None (their
        # provenance-flat placement), and canonical Video\ files stay put.
        wrong_media = {m.casefold() for m in MEDIA_LABELS} - _media_top_segments(cfg)
        if already.split("/")[0].casefold() in wrong_media:
            video_top = lay.movies_root.replace("\\", "/").split("/")[0]
            if re.search(r"(^|[\\/ _-])devotional", already, re.IGNORECASE):
                return Route(_native(video_top, "Devotional", filename),
                             "route:video:devotional")
            return Route(_native(video_top, "Unsorted", filename),
                         "route:video:unsorted")
        return None                     # video with no derivable identity

    if label == "Audio":
        if _inside(already, lay.music_root) is not None:
            # Under the music root NOTHING moves: Artist/Album trees,
            # Compilations, language folders — all are valid structure the
            # router has no business re-deriving (review finding: the earlier
            # language-only guard flattened artist trees to Other/).
            return Route(relpath, "route:music:already-placed")
        # Deterministic audio triage (audioclass): not all audio is music. A
        # file already in a canonical non-music audio home stays; otherwise a
        # WhatsApp/recorder voice note -> Audio/Personal, stage comedy ->
        # Spoken_Word/Comedy, a discourse/lecture -> Spoken_Word/Discourse, junk
        # (macOS forks, hash caches) stays for the junk channel. Only a 'song'
        # proceeds to language-based music placement.
        audio_top = lay.music_root.replace("\\", "/").split("/")[0]
        for home in ("Spoken_Word", "Personal", "System_Sounds"):
            if _inside(already, f"{audio_top}/{home}") is not None:
                return Route(relpath, "route:audio:already-placed")
        aclass = audioclass.classify(filename, cfg.audio_patterns)
        if aclass == "voice":
            return Route(_native(audio_top, "Personal", filename),
                         "route:audio:voice")
        if aclass == "comedy":
            return Route(_native(audio_top, "Spoken_Word", "Comedy", filename),
                         "route:audio:comedy")
        if aclass == "spoken":
            return Route(_native(audio_top, "Spoken_Word", "Discourse", filename),
                         "route:audio:spoken")
        if aclass == "junk":
            return None
        if aclass is None:
            # No title at all (a numeric/recovery carve) — not a song, so it is
            # NOT placed by language. It goes to the Music\Unsorted shelf (the
            # drain rule moves it only when consolidating a wrong media root).
            return Route(_native(lay.music_root, "Unsorted", filename),
                         "route:music:unsorted")
        # A 'song' — finer Music sub-bucket first: a bare 'Track NN' has no
        # placeable title (-> Music\Unsorted, a shelf the drain rule gates); a
        # bhajan/stotram is devotional regardless of language.
        sbucket = audioclass.song_bucket(filename)
        if sbucket == "lost":
            return Route(_native(lay.music_root, "Unsorted", filename),
                         "route:music:unsorted")
        lang, lrule = _language_segment(cfg, relpath, hints)
        if sbucket == "devotional":
            # Devotional\<Lang> when a real language is known; Devotional\Unsorted
            # when only the default shelf applies (language not clear).
            if lrule == "lang:default":
                return Route(
                    _native(lay.music_root, "Devotional", "Unsorted", filename),
                    "route:music:devotional-unsorted")
            return Route(_native(lay.music_root, "Devotional", *lang, filename),
                         f"route:music:devotional:{lrule}")
        if lrule == "lang:hint":
            # The language came from a PER-FILE critic hint, not the path — the
            # folder carries no language marker, i.e. a mixed-language
            # compilation or a provenance dump ('E_HDD2_Part1' with a Tamil
            # song beside a Hindi one). Route the song FLAT under ITS OWN
            # language; the parent folder is NOT an artist/album, so it must not
            # leak into the home. Each file scatters to its own language.
            return Route(_native(lay.music_root, *lang, filename),
                         "route:music:hint-flat")
        # A song under a wrong media root (Audio\<dump>\...) routes FLAT whenever
        # its would-be parent grouping is provenance, never an album, so no
        # provenance folder leaks into the curated tree (the original complaint):
        # that is the top dump folder itself (depth 2 under the root) OR any
        # backup/drive-prefixed segment at any depth. A real album parent (Roja/)
        # is kept; the provenance ancestors above it are dropped anyway, since
        # only the immediate parent is ever carried.
        parts = already.split("/")
        in_media_root = parts[0].casefold() in {m.casefold() for m in MEDIA_LABELS}
        parent = parts[-2] if len(parts) >= 2 else ""
        # len <= 3 routes FLAT: depth 3 is the dump folder itself, and depth 2
        # sits directly under the media top — whose LABEL ('Audio') must not
        # ride along as a phantom album folder.
        if in_media_root and (len(parts) <= 3 or _PROVENANCE_SEG.search(parent)):
            tail = parts[-1:]
        else:
            tail = parts[-2:]
            if len(tail) == 2 and tail[0].casefold() in lang_names:
                tail = tail[-1:]        # don't double a language segment
        if lrule == "lang:default":
            # C30: we know it's a song, but not what and not what language.
            # The previous `route:music:lang:default` route landed the file in
            # Music\<default-language>\ — pretending the default language slot
            # was a language decision. The honest home is Music\Unsorted\,
            # named for the actual state of knowledge. Same C19 shelf-block
            # class in unscoped runs; a scoped drain now lands unsortable songs
            # in a bucket that says so, keeping any genuine album parent intact.
            return Route(_native(lay.music_root, "Unsorted", *tail),
                         "route:music:unsorted")
        return Route(_native(lay.music_root, *lang, *tail),
                     f"route:music:{lrule}")

    if label in ("Photos", "Images"):
        images_top = lay.photos_root.replace("\\", "/").split("/")[0]
        # Canonical non-Photos image homes stay put (the same already-placed
        # posture as audio's non-music homes).
        for home in ("WhatsApp", "Screenshots", "Graphics_Icons", "Personal"):
            if _inside(already, f"{images_top}/{home}") is not None:
                return Route(relpath, "route:image:already-placed")
        inner_ph = _inside(already, lay.photos_root)
        if inner_ph is not None:
            seg = inner_ph.split("/", 1)[0]
            is_year_dir = seg.isdigit() and len(seg) == 4
            in_unsorted = seg.casefold() == "unsorted"
            # Year correction applies only to files DIRECTLY under the year
            # folder: a photo inside an album ('2013/Wedding Album/x.jpg') is
            # the human's grouping — correcting its year would tear the album
            # apart one file at a time (and used to FLATTEN the album path).
            if is_year_dir and inner_ph.count("/") == 1 and hints \
                    and hints.year and hints.year != int(seg):
                return Route(_native(lay.photos_root, str(hints.year), filename),
                             "route:photo:exif-year-correction")
            if in_unsorted and hints and hints.year:
                return Route(_native(lay.photos_root, str(hints.year), filename),
                             "route:photo:exif-year")
            # Everything else under the photos root — year folders, albums
            # ('Wedding 2019/'), whatever the human built — stays (review
            # finding: the year-folder-only guard scattered albums to Unsorted).
            return Route(relpath, "route:photo:already-placed")
        # Not yet in a canonical image home. Deterministic image triage
        # (imgclass): not all images are photos — WhatsApp -> Images\WhatsApp, a
        # screen capture -> Images\Screenshots, a web/app UI sprite (icons,
        # spinners, logos, .gif) -> Images\Graphics_Icons.
        iclass = imgclass.classify(filename, cfg.image_patterns)
        if iclass == "whatsapp":
            return Route(_native(images_top, "WhatsApp", filename),
                         "route:image:whatsapp")
        if iclass == "screenshot":
            return Route(_native(images_top, "Screenshots", filename),
                         "route:image:screenshot")
        if iclass == "ui":
            return Route(_native(images_top, "Graphics_Icons", filename),
                         "route:image:ui")
        # A photo: an EXIF year (Hints) or, failing that, a name-embedded epoch
        # timestamp gives Photos\<Year> — a home. Otherwise the Unsorted shelf
        # (the drain rule moves it only when consolidating a wrong media root).
        if hints and hints.year:
            return Route(_native(lay.photos_root, str(hints.year), filename),
                         "route:photo:exif-year")
        nyear = imgclass.name_year(filename)
        if nyear:
            return Route(_native(lay.photos_root, str(nyear), filename),
                         "route:photo:name-year")
        return Route(_native(lay.photos_root, "Unsorted", filename),
                     "route:photo:unsorted")

    if label == "Comics":
        # A comic (.cbr/.cbz) is its own Jellyfin-style library type. Group by
        # series from the filename; already under the comics root -> stays.
        if _inside(already, lay.comics_root) is not None:
            # Already under comics_root — check if the immediate series
            # folder has a normalizable trailing issue number
            # (`Star Wars 01` -> `Star Wars`); reroute to consolidate.
            inner = _inside(already, lay.comics_root)
            parts = inner.split("/")
            if len(parts) >= 2:
                current_series = parts[-2]
                normalized = _normalize_comic_series(current_series)
                if normalized != current_series:
                    return Route(
                        _native(lay.comics_root, normalized, filename),
                        "route:comic:series-normalize")
            return Route(relpath, "route:comic:already-placed")
        series = _comic_series(filename)
        if series:
            return Route(_native(lay.comics_root, series, filename),
                         "route:comic:series")
        return Route(_native(lay.comics_root, filename), "route:comic:flat")

    if label == "Ebooks":
        # C43 (P17): a book is an identity, not an extension. Author folders
        # are "Last, First" (owner-locked, particle-aware); Series groups
        # under the author; identity comes from hints (a critic/subagent
        # judgment or bookmeta-augmented embedded metadata) FIRST, then a pure
        # filename parse — never a router-side guess.
        stem = os.path.splitext(filename)[0]
        ext = os.path.splitext(filename)[1]
        h_author = hints.book_author if hints else None
        h_title = hints.book_title if hints else None
        h_series = hints.book_series if hints else None
        h_index = hints.book_index if hints else None
        if h_author or h_title or h_series or h_index is not None:
            author, title, series, index = h_author, h_title, h_series, h_index
        else:
            parsed = bookmeta.parse_name(stem)
            author, title = parsed["author"], parsed["title"]
            series, index = parsed["series"], parsed["series_index"]

        inner = _inside(already, lay.ebooks_root)
        if inner is not None:
            # Already under Books\ — the reshelve exception (comics
            # series-normalize precedent): a file sitting in the Unsorted
            # shelf whose identity NOW resolves an author gets re-derived.
            # Everything else correctly shelved elsewhere is that tree's
            # business and stays (C15/C18 — never re-derive a placed book).
            parts = inner.split("/")
            shelf = bookmeta.shelf_author(author) if author else None
            if parts[0].casefold() == "unsorted" and shelf:
                dest = _book_dest(lay.ebooks_root, shelf, series, index,
                                  title or stem, ext)
                return Route(dest, "route:book:reshelve")
            return Route(relpath, "route:book:already-placed")

        shelf = bookmeta.shelf_author(author) if author else None
        if shelf:
            rule = "route:book:author-series" if series else "route:book:author"
            dest = _book_dest(lay.ebooks_root, shelf, series, index,
                              title or stem, ext)
            return Route(dest, rule)
        return Route(_native(lay.ebooks_root, "Unsorted", filename),
                     "route:book:unsorted")

    # C35 — Presentations, Spreadsheets, Archives, Installers all get canonical
    # homes now (previously they returned None and files stayed wherever the
    # predecessor dumped them, giving parallel top-level `Presentations\` and
    # `Spreadsheets\` piles). Immediate parent grouping is preserved when it
    # isn't a provenance/unsorted folder — an `Excel\Budget2024\` album keeps
    # its shape; a bare `Unsorted\` is dropped.
    for lbl, root in (("Presentations", lay.presentations_root),
                      ("Spreadsheets", lay.spreadsheets_root),
                      ("Archives", lay.archives_root),
                      ("Installers", lay.installers_root)):
        if label != lbl:
            continue
        inner = _inside(already, root)
        if inner is not None:
            # inside root — but a top-level 'Unsorted' shelf is not a home,
            # lift the file out; anything else stays.
            parts = inner.split("/")
            if parts and parts[0].casefold() == "unsorted":
                remainder = "/".join(parts[1:])
                return Route(_native(root, remainder) if remainder
                             else _native(root, filename),
                             "route:bucket:unshelf")
            return Route(relpath, "route:bucket:already-placed")
        # not inside root — route in, preserving a genuine parent grouping
        parts = already.split("/")
        parent = parts[-2] if len(parts) >= 2 else ""
        if parent and parent.casefold() != "unsorted" \
                and not _PROVENANCE_SEG.search(parent):
            return Route(_native(root, parent, filename), "route:bucket:keep")
        return Route(_native(root, filename), "route:bucket:flat")

    # Non-media buckets (Documents, Backups, Code, ...): no content-derived
    # route — the caller's provenance-preserving convention applies (organize)
    # or the file stays put (reorganize).
    return None


@dataclass(frozen=True)
class Coverage:
    total: int
    matched: int
    unmatched_count: int
    unmatched_pct: float
    threshold_pct: float
    top_unmatched_tokens: list[tuple[str, int]]
    blocked: bool


def coverage(cfg: Config, relpaths: Iterable[str]) -> Coverage:
    """Coverage over an iterable of relpaths. blocked is a plan-build stopper
    (exit 5): unmatched share strictly above cfg.max_unmatched_pct."""
    total = matched = 0
    tokens: Counter[str] = Counter()
    for rel in relpaths:
        total += 1
        if bucket_for(cfg, rel) is not None:
            matched += 1
            continue
        base = os.path.basename(rel).lower()
        stem, ext = os.path.splitext(base)
        if ext:
            tokens[ext] += 1
        for tok in _TOKEN_RE.split(stem):
            if tok:
                tokens[tok] += 1
    unmatched = total - matched
    pct = (unmatched / total * 100.0) if total else 0.0
    return Coverage(
        total=total,
        matched=matched,
        unmatched_count=unmatched,
        unmatched_pct=pct,
        threshold_pct=cfg.max_unmatched_pct,
        top_unmatched_tokens=tokens.most_common(25),
        blocked=pct > cfg.max_unmatched_pct,
    )
