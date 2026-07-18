"""Configuration: one TOML file is the single source of truth (defect L6).

Startup validation on every command (defect L8):
  - unknown keys anywhere are refused (a typo cannot silently disable a rule);
  - every ``enabled = true`` source root and the library root must be reachable,
    else the error names the two legal remedies (fix the drive / set enabled=false);
  - the workspace (.mlo) must not live under any root the engine scans or mutates.

No other module may embed data lists (candidates, extensions, protected paths):
they all read the tables parsed here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass, field

from . import staging as stagingmod
from . import winpath


class ConfigError(Exception):
    """Invalid or unsafe configuration. CLI maps this to exit 2."""


# Recognized schema: section -> allowed keys (None = free-form table values).
_SCHEMA: dict[str, set[str] | None] = {
    "library": {"root"},
    "sources": {"name", "root", "enabled"},        # array of tables
    "staging": None,                               # drive letter OR path prefix -> staging root
    "protected": {"substrings", "drives"},
    "junk": {"zero_byte", "names", "extensions"},
    "classify": {"max_unmatched_pct", "name_patterns", "audio_patterns",
                 "image_patterns"},
    "taxonomy": {"buckets"},                       # buckets: label -> [exts]
    "layout": {"movies_root", "tv_root", "music_root", "photos_root",
               "personal_root", "comics_root", "ebooks_root",
               "presentations_root", "spreadsheets_root",
               "archives_root", "installers_root",
               "default_language", "language_folders",
               "languages", "subtypes"},
    "layout.languages": None,                      # Language -> [path tokens]
    "layout.subtypes": None,                       # finer media kind -> sub-root
    "llm": {"enabled", "chain", "critics_chain", "local"},
    "llm.local": {"enabled", "url", "model", "num_ctx", "timeout_s",
                  "keep_alive", "reasoning_effort"},
    "containers": {"homes", "patterns"},           # C33 semantic containers
    "containers.homes": None,                      # kind -> destination root
    "containers.patterns": None,                   # kind -> [segment regexes]
    # P21/B1/B8: opt-in enrichment connectors. Keys (API keys) are NEVER
    # accepted here (secrets stay out of mlo.toml, owner directive) — only
    # endpoints/toggles. See agent/llm.py's MLO_*_KEY env-var precedent.
    "enrich": {"searxng_url", "tmdb_enabled", "id3_enabled",
               "opensubtitles_enabled"},
}


@dataclass(frozen=True)
class Source:
    name: str
    root: str
    enabled: bool = True


@dataclass(frozen=True)
class LocalLLM:
    enabled: bool = False
    url: str = "http://localhost:11434"
    model: str = "gpt-oss:20b"
    num_ctx: int = 8192
    timeout_s: int = 240
    keep_alive: str = "30m"
    reasoning_effort: str = "medium"


@dataclass(frozen=True)
class Layout:
    """Jellyfin-compatible placement, on by default (user directive, v0.2).
    Segment roots are forward-slash relative paths inside the library; the
    router converts to native separators. default_language is EXPLICIT config
    data — routing to it carries rule provenance, so it is not an implicit
    'Other' bucket (defect L4)."""
    movies_root: str = "Video/Movies"
    tv_root: str = "Video/TV_Shows"
    music_root: str = "Audio/Music"
    photos_root: str = "Images/Photos"
    personal_root: str = "Video/Personal"
    comics_root: str = "Comics"                 # .cbr/.cbz -> Comics/<Series>/
    ebooks_root: str = "Books"                  # C43: ebooks -> Books/<Last, First>/
    # C35 non-media bucket roots — files with these ext-buckets otherwise fall
    # through to `return None` in route(); giving them canonical homes turns
    # dumped piles at `Presentations\Unsorted\` into `Documents\Presentations\`.
    presentations_root: str = "Documents/Presentations"
    spreadsheets_root: str = "Documents/Spreadsheets"
    archives_root: str = "Archives"             # top-level, but organized
    installers_root: str = "Installers"         # top-level
    default_language: str = "Other"
    language_folders: bool = True
    languages: dict[str, tuple[str, ...]] = field(default_factory=lambda: {
        "English": ("english", "hollywood"),
        "Tamil": ("tamil", "kollywood"),
        "Hindi": ("hindi", "bollywood"),
        "Telugu": ("telugu", "tollywood"),
        "Classical": ("classical", "carnatic", "instrumental"),
    })
    # Finer media types (§7): a critic-assigned media_kind -> a sub-root inside
    # the library. Empty by default (coarse Movies/TV/Music/Photos only); the
    # finer-type FOLDER NAMES live here in config, never in engine code (L6).
    subtypes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LLM:
    enabled: bool = False
    chain: tuple[str, ...] = ()
    # Chain used by the critic panel (and pilot) when set — critics carry the
    # heaviest judgment (file-disposition hints), so they may warrant a stronger
    # chain than routine tasks. Resolution: CLI --chain > critics_chain > chain.
    critics_chain: tuple[str, ...] = ()
    local: LocalLLM = field(default_factory=LocalLLM)


@dataclass(frozen=True)
class Enrich:
    """Opt-in enrichment connectors (P21/B1/B8). Endpoints/toggles only — API
    keys are read from environment variables (MLO_TMDB_KEY,
    MLO_OPENSUBTITLES_KEY), never accepted here; secrets stay out of
    mlo.toml (owner directive)."""
    searxng_url: str = ""
    tmdb_enabled: bool = False
    id3_enabled: bool = False
    opensubtitles_enabled: bool = False


@dataclass(frozen=True)
class Config:
    library_root: str
    sources: tuple[Source, ...]
    staging: dict[str, str]                 # drive letter OR abs path prefix -> staging root
    protected_substrings: tuple[str, ...]
    protected_drives: tuple[str, ...]
    junk_zero_byte: bool
    junk_names: tuple[str, ...]
    junk_extensions: tuple[str, ...]
    max_unmatched_pct: float
    taxonomy: dict[str, tuple[str, ...]]    # bucket label -> extensions
    layout: Layout
    llm: LLM
    config_hash: str
    path: str
    name_patterns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    audio_patterns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    image_patterns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # C33 semantic containers: config EXTENSIONS over containers.py built-ins
    container_homes: dict[str, str] = field(default_factory=dict)
    container_patterns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    enrich: Enrich = field(default_factory=Enrich)

    def source(self, name: str) -> Source:
        for s in self.sources:
            if s.name == name:
                return s
        raise ConfigError(f"unknown source '{name}' (declared: "
                          f"{', '.join(s.name for s in self.sources) or 'none'})")


def _unknown_keys(raw: dict) -> list[str]:
    bad: list[str] = []
    for section, val in raw.items():
        if section not in _SCHEMA:
            bad.append(section)
            continue
        # A section of the wrong TOML type (e.g. `llm = "local"`) must refuse
        # with a remedy, not crash the key-walker with an AttributeError.
        if section == "sources":
            if not isinstance(val, list):
                raise ConfigError(
                    f"[[sources]] must be an array of tables, got "
                    f"{type(val).__name__}")
        elif not isinstance(val, dict):
            raise ConfigError(
                f"[{section}] must be a table, got {type(val).__name__}")
        allowed = _SCHEMA[section]
        if section == "sources":
            for i, item in enumerate(val if isinstance(val, list) else []):
                for k in item:
                    if k not in allowed:
                        bad.append(f"sources[{i}].{k}")
        elif section == "llm":
            for k, v in val.items():
                if k not in allowed:
                    bad.append(f"llm.{k}")
            for sub in ("local",):
                sub_allowed = _SCHEMA.get(f"llm.{sub}")
                if sub in val and sub_allowed is not None:
                    for k in val[sub]:
                        if k not in sub_allowed:
                            bad.append(f"llm.{sub}.{k}")
        elif section == "taxonomy":
            for k in val:
                if k not in allowed:
                    bad.append(f"taxonomy.{k}")
        elif allowed is not None and isinstance(val, dict):
            for k in val:
                if k not in allowed:
                    bad.append(f"{section}.{k}")
    return bad


def _config_hash(raw: dict) -> str:
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()


def load(path: str) -> Config:
    """Parse and structurally validate (no filesystem checks — see validate())."""
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config not found: {path} (run `mlo init` to create one)")
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config parse error in {path}: {e}")

    bad = _unknown_keys(raw)
    if bad:
        raise ConfigError(
            "unknown config keys (typo?): " + ", ".join(sorted(bad)))

    lib = raw.get("library", {})
    if not lib.get("root"):
        raise ConfigError("missing required key: library.root")

    sources = []
    seen = set()
    for item in raw.get("sources", []):
        if "name" not in item or "root" not in item:
            raise ConfigError("every [[sources]] entry needs name and root")
        if item["name"] in seen:
            raise ConfigError(f"duplicate source name: {item['name']}")
        seen.add(item["name"])
        sources.append(Source(item["name"], item["root"],
                              bool(item.get("enabled", True))))

    # Staging keys (P21/A4): a single Windows drive letter (case-folded, as
    # before), OR an absolute path prefix — a UNC share ('\\\\NAS\\Share') or
    # a POSIX mount ('/mnt/media') — kept VERBATIM (POSIX paths are
    # case-sensitive; folding one would silently break matching).
    staging: dict[str, str] = {}
    for raw_key, v in raw.get("staging", {}).items():
        k = str(raw_key)
        if stagingmod.is_drive_letter_key(k):
            staging[k.upper()] = str(v)
        elif stagingmod.is_staging_prefix_key(k):
            staging[k] = str(v)
        else:
            raise ConfigError(
                f"staging keys must be a single drive letter or an absolute "
                f"path prefix (a UNC share or a POSIX mount), got '{k}'")

    prot = raw.get("protected", {})
    for s_ in prot.get("substrings", []):
        if not str(s_).strip():
            raise ConfigError(
                "protected.substrings must not contain empty strings — '' "
                "matches every path in some consumers and no path in others "
                "(walkers prune on substring containment)")
    junk = raw.get("junk", {})
    classify = raw.get("classify", {})
    tax = {label: tuple(str(e).lower() for e in exts)
           for label, exts in raw.get("taxonomy", {}).get("buckets", {}).items()}
    for label, exts in tax.items():
        for e in exts:
            if not e.startswith("."):
                raise ConfigError(f"taxonomy.buckets.{label}: '{e}' must start with '.'")

    lay_raw = raw.get("layout", {})
    lay_defaults = Layout()
    for key in ("movies_root", "tv_root", "music_root", "photos_root",
                "personal_root", "comics_root", "ebooks_root",
                "presentations_root", "spreadsheets_root",
                "archives_root", "installers_root"):
        val = str(lay_raw.get(key, "")) if key in lay_raw else ""
        if val and (":" in val or val.startswith(("/", "\\"))
                    or ".." in val.replace("\\", "/").split("/")):
            raise ConfigError(
                f"layout.{key} must be a relative path inside the library "
                f"(forward slashes, no drive, no '..'): got {val!r}")
    sub_raw = lay_raw.get("subtypes", {})
    if not isinstance(sub_raw, dict):
        raise ConfigError("[layout.subtypes] must be a table of kind = 'sub/root'")
    subtypes: dict[str, str] = {}
    for kind, sub in sub_raw.items():
        val = str(sub)
        if ":" in val or val.startswith(("/", "\\")) \
                or ".." in val.replace("\\", "/").split("/"):
            raise ConfigError(
                f"layout.subtypes.{kind} must be a relative path inside the "
                f"library (forward slashes, no drive, no '..'): got {val!r}")
        subtypes[str(kind)] = val
    layout = Layout(
        movies_root=str(lay_raw.get("movies_root", lay_defaults.movies_root)),
        tv_root=str(lay_raw.get("tv_root", lay_defaults.tv_root)),
        music_root=str(lay_raw.get("music_root", lay_defaults.music_root)),
        photos_root=str(lay_raw.get("photos_root", lay_defaults.photos_root)),
        personal_root=str(lay_raw.get("personal_root", lay_defaults.personal_root)),
        comics_root=str(lay_raw.get("comics_root", lay_defaults.comics_root)),
        ebooks_root=str(lay_raw.get("ebooks_root", lay_defaults.ebooks_root)),
        presentations_root=str(lay_raw.get(
            "presentations_root", lay_defaults.presentations_root)),
        spreadsheets_root=str(lay_raw.get(
            "spreadsheets_root", lay_defaults.spreadsheets_root)),
        archives_root=str(lay_raw.get(
            "archives_root", lay_defaults.archives_root)),
        installers_root=str(lay_raw.get(
            "installers_root", lay_defaults.installers_root)),
        default_language=str(lay_raw.get("default_language",
                                         lay_defaults.default_language)),
        language_folders=bool(lay_raw.get("language_folders",
                                          lay_defaults.language_folders)),
        languages={str(k): tuple(str(t).lower() for t in v)
                   for k, v in lay_raw.get("languages",
                                           lay_defaults.languages).items()},
        subtypes=subtypes,
    )

    # [classify.name_patterns]: media kind -> filename regexes, consulted
    # before the built-in NAME_PATTERNS (user knowledge wins). Validated
    # here so a typo'd kind or regex fails at startup, not mid-classify.
    np_raw = classify.get("name_patterns", {})
    if not isinstance(np_raw, dict):
        raise ConfigError("[classify.name_patterns] must be a table of "
                          "kind = [regexes]")
    name_patterns: dict[str, tuple[str, ...]] = {}
    for kind, regexes in np_raw.items():
        if kind not in ("movie", "tv", "personal", "music", "junk"):
            raise ConfigError(
                f"[classify.name_patterns] unknown kind '{kind}' "
                f"(allowed: movie, tv, personal, music, junk)")
        if not isinstance(regexes, list):
            raise ConfigError(
                f"[classify.name_patterns] {kind} must be a list of regexes")
        for rx in regexes:
            try:
                re.compile(str(rx))
            except re.error as e:
                raise ConfigError(
                    f"[classify.name_patterns] {kind}: bad regex {rx!r}: {e}")
        name_patterns[kind] = tuple(str(rx) for rx in regexes)

    # [classify.audio_patterns]: audioclass kind -> filename regexes, consulted
    # BEFORE the built-in audio triage (a library's own conventions win — e.g. a
    # stage-comedy artist that should route to Spoken_Word, not be read as a
    # song). Kinds mirror audioclass; 'song' is the fallthrough, not a pattern.
    ap_raw = classify.get("audio_patterns", {})
    if not isinstance(ap_raw, dict):
        raise ConfigError("[classify.audio_patterns] must be a table of "
                          "kind = [regexes]")
    audio_patterns: dict[str, tuple[str, ...]] = {}
    for kind, regexes in ap_raw.items():
        # devotional/lost are song_bucket's finer Music sub-buckets — the
        # extension path the built-in devotional list promises (e.g. a
        # padam/javali library the built-ins deliberately omit).
        if kind not in ("voice", "spoken", "comedy", "junk",
                        "devotional", "lost"):
            raise ConfigError(
                f"[classify.audio_patterns] unknown kind '{kind}' "
                f"(allowed: voice, spoken, comedy, junk, devotional, lost)")
        if not isinstance(regexes, list):
            raise ConfigError(
                f"[classify.audio_patterns] {kind} must be a list of regexes")
        for rx in regexes:
            try:
                re.compile(str(rx))
            except re.error as e:
                raise ConfigError(
                    f"[classify.audio_patterns] {kind}: bad regex {rx!r}: {e}")
        audio_patterns[kind] = tuple(str(rx) for rx in regexes)

    # [classify.image_patterns]: imgclass kind -> filename regexes, consulted
    # BEFORE the built-in image triage (the module's documented config seam).
    ip_raw = classify.get("image_patterns", {})
    if not isinstance(ip_raw, dict):
        raise ConfigError("[classify.image_patterns] must be a table of "
                          "kind = [regexes]")
    image_patterns: dict[str, tuple[str, ...]] = {}
    for kind, regexes in ip_raw.items():
        if kind not in ("whatsapp", "ui", "screenshot"):
            raise ConfigError(
                f"[classify.image_patterns] unknown kind '{kind}' "
                f"(allowed: whatsapp, ui, screenshot)")
        if not isinstance(regexes, list):
            raise ConfigError(
                f"[classify.image_patterns] {kind} must be a list of regexes")
        for rx in regexes:
            try:
                re.compile(str(rx))
            except re.error as e:
                raise ConfigError(
                    f"[classify.image_patterns] {kind}: bad regex {rx!r}: {e}")
        image_patterns[kind] = tuple(str(rx) for rx in regexes)

    # [containers] (C33): homes = kind -> destination root inside the library;
    # patterns = kind -> folder-SEGMENT regexes, consulted BEFORE the built-ins
    # in containers.py (a library's own conventions win). A patterns kind with
    # no home in the merged table is a config error — a container that matches
    # but cannot land anywhere would be a silent no-op (L8 posture).
    from . import containers as containersmod
    cont_raw = raw.get("containers", {}) or {}
    homes_raw = cont_raw.get("homes", {})
    if not isinstance(homes_raw, dict):
        raise ConfigError("[containers.homes] must be a table of "
                          "kind = 'dest/root'")
    container_homes: dict[str, str] = {}
    for kind, dest in homes_raw.items():
        val = str(dest)
        if ":" in val or val.startswith(("/", "\\")) \
                or ".." in val.replace("\\", "/").split("/"):
            raise ConfigError(
                f"containers.homes.{kind} must be a relative path inside the "
                f"library (got {val!r})")
        container_homes[str(kind)] = val
    pat_raw = cont_raw.get("patterns", {})
    if not isinstance(pat_raw, dict):
        raise ConfigError("[containers.patterns] must be a table of "
                          "kind = [regexes]")
    container_patterns: dict[str, tuple[str, ...]] = {}
    merged_homes = {**containersmod.builtin_homes(), **container_homes}
    for kind, regexes in pat_raw.items():
        if kind not in merged_homes:
            raise ConfigError(
                f"[containers.patterns] kind '{kind}' has no destination — "
                f"add containers.homes.{kind}")
        if not isinstance(regexes, list):
            raise ConfigError(
                f"[containers.patterns] {kind} must be a list of regexes")
        for rx in regexes:
            try:
                re.compile(str(rx))
            except re.error as e:
                raise ConfigError(
                    f"[containers.patterns] {kind}: bad regex {rx!r}: {e}")
        container_patterns[str(kind)] = tuple(str(rx) for rx in regexes)

    llm_raw = raw.get("llm", {})
    local_raw = llm_raw.get("local", {})

    def _chain(key: str) -> tuple[str, ...]:
        # `chain = "local"` would tuple()-explode into per-character "model
        # names" ('l','o','c','a','l') and fail at call time with nonsense —
        # refuse loudly at load instead (L8 posture).
        v = llm_raw.get(key, [])
        if isinstance(v, str):
            raise ConfigError(
                f"[llm] {key} must be an array (e.g. [\"local\"]), got a "
                f"string — write {key} = [{v!r}]")
        return tuple(str(e) for e in v)

    def _num(table: str, raw_tbl: dict, key: str, conv, default):
        try:
            return conv(raw_tbl.get(key, default))
        except (TypeError, ValueError):
            raise ConfigError(
                f"[{table}] {key} must be a number, got "
                f"{raw_tbl.get(key)!r}")

    llm = LLM(
        enabled=bool(llm_raw.get("enabled", False)),
        chain=_chain("chain"),
        critics_chain=_chain("critics_chain"),
        local=LocalLLM(
            enabled=bool(local_raw.get("enabled", False)),
            url=str(local_raw.get("url", LocalLLM.url)),
            model=str(local_raw.get("model", LocalLLM.model)),
            num_ctx=_num("llm.local", local_raw, "num_ctx", int,
                         LocalLLM.num_ctx),
            timeout_s=_num("llm.local", local_raw, "timeout_s", int,
                           LocalLLM.timeout_s),
            keep_alive=str(local_raw.get("keep_alive", LocalLLM.keep_alive)),
            reasoning_effort=str(local_raw.get("reasoning_effort",
                                               LocalLLM.reasoning_effort)),
        ),
    )

    enrich_raw = raw.get("enrich", {})
    _sx = str(enrich_raw.get("searxng_url", ""))
    if _sx and not _sx.startswith(("http://", "https://")):
        raise ConfigError(
            f"enrich.searxng_url must start with http:// or https:// "
            f"(got {_sx!r}) — a scheme-less URL fails every query silently")
    enrich = Enrich(
        searxng_url=_sx,
        tmdb_enabled=bool(enrich_raw.get("tmdb_enabled", False)),
        id3_enabled=bool(enrich_raw.get("id3_enabled", False)),
        opensubtitles_enabled=bool(
            enrich_raw.get("opensubtitles_enabled", False)),
    )

    return Config(
        library_root=str(lib["root"]),
        sources=tuple(sources),
        staging=staging,
        protected_substrings=tuple(str(s).lower() for s in prot.get("substrings", [])),
        protected_drives=tuple(str(d).upper() for d in prot.get("drives", [])),
        junk_zero_byte=bool(junk.get("zero_byte", True)),
        junk_names=tuple(str(n).lower() for n in junk.get("names", [])),
        junk_extensions=tuple(str(e).lower() for e in junk.get("extensions", [])),
        max_unmatched_pct=_num("classify", classify, "max_unmatched_pct",
                               float, 5.0),
        name_patterns=name_patterns,
        audio_patterns=audio_patterns,
        image_patterns=image_patterns,
        container_homes=container_homes,
        container_patterns=container_patterns,
        taxonomy=tax,
        layout=layout,
        llm=llm,
        config_hash=_config_hash(raw),
        path=os.path.abspath(path),
        enrich=enrich,
    )


def validate(cfg: Config, workspace_dir: str) -> list[str]:
    """Filesystem validation (defect L8). Raises ConfigError on hard failures;
    returns informational notes (e.g. disabled sources) otherwise."""
    notes: list[str] = []

    if not os.path.isdir(winpath.to_long(cfg.library_root)):
        raise ConfigError(
            f"library.root is not reachable: {cfg.library_root}")

    for s in cfg.sources:
        if not s.enabled:
            notes.append(f"source '{s.name}' is disabled (enabled = false) — skipped")
            continue
        if not os.path.isdir(winpath.to_long(s.root)):
            raise ConfigError(
                f"source '{s.name}' root is not reachable: {s.root}\n"
                f"  remedies: reattach the drive, or set enabled = false for it "
                f"(dead infrastructure must be explicit, never commented out)")

    ws = os.path.abspath(workspace_dir)
    mutated_roots = [cfg.library_root, *[s.root for s in cfg.sources if s.enabled],
                     *cfg.staging.values()]
    for root in mutated_roots:
        if winpath.is_under(ws, root):
            raise ConfigError(
                f"workspace {ws} must not live under a scanned/mutated root ({root})")

    for drive, root in cfg.staging.items():
        # Prefix keys (UNC/POSIX mounts) have no single drive_of() identity to
        # cross-check by construction (P21/A4) — this consistency check only
        # applies to legacy single-letter drive keys.
        if not stagingmod.is_drive_letter_key(drive):
            continue
        got = winpath.drive_of(root)
        if got and got != drive:
            raise ConfigError(
                f"staging.{drive} points at a different drive's path: {root}")

    # Placement safety (defect C4): a staging root INSIDE the library would let
    # disposal-bound files be indexed as library content — a source's only other
    # copy could then verdict ORGANIZED and be staged too, and disposal would
    # destroy the last copy. Forbid it. (Staging inside a source root is allowed
    # and normal — same-drive staging — because the scanners prune it.)
    for drive, root in cfg.staging.items():
        if winpath.is_under(root, cfg.library_root):
            raise ConfigError(
                f"staging.{drive} ({root}) must not live inside the library "
                f"root ({cfg.library_root}) — staged files would be indexed as "
                f"library content")
        if winpath.is_under(cfg.library_root, root):
            raise ConfigError(
                f"library root ({cfg.library_root}) must not live inside "
                f"staging.{drive} ({root})")
        for s in cfg.sources:
            if s.enabled and winpath.is_under(s.root, root):
                raise ConfigError(
                    f"source '{s.name}' root ({s.root}) must not live inside "
                    f"staging.{drive} ({root})")

    # A source nested under the library would be entirely pruned from its own
    # scan (the scanner excludes the library from source walks, C4), silently
    # emptying it. Refuse it (C10).
    for s in cfg.sources:
        if s.enabled and winpath.is_under(s.root, cfg.library_root):
            raise ConfigError(
                f"source '{s.name}' root ({s.root}) must not live inside the "
                f"library root ({cfg.library_root})")

    return notes
