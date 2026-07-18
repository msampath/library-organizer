"""Onboarding interview (W5-U2) — the parameterized config surface made runnable.

`mlo init --interview` asks the owner-specific questions from general-principles
Part C — the sacrosanct folders, off-limits roots, source and library roots,
languages, local model — and writes a valid mlo.toml. A sacrosanct folder is
never hardcoded; it is an ANSWER to "what must never be touched?". The static
Jellyfin taxonomy/layout defaults are rendered verbatim so the result passes
`mlo check` unchanged.

build_config_toml is PURE (answers dict -> TOML string); run_interview is the
thin interactive driver with an injectable input function so it is testable.
"""
from __future__ import annotations

import re

from . import staging as stagingmod
from . import winpath

# Static Jellyfin defaults (the same scheme the starter ships) — rendered into
# every generated config so it validates out of the box.
_TAXONOMY = """[taxonomy.buckets]
Video      = [".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".mpg", ".vob", ".3gp"]
Audio      = [".mp3", ".flac", ".m4a", ".wav", ".ogg", ".opus", ".amr", ".wma"]
Photos     = [".jpg", ".jpeg", ".png", ".gif", ".heic", ".webp",
              ".raw", ".dng", ".kdc", ".cr2", ".nef", ".arw", ".orf", ".raf", ".rw2"]
Documents  = [".pdf", ".docx", ".doc", ".xlsx", ".txt", ".epub", ".md"]
Code       = [".py", ".js", ".ts", ".cs", ".java", ".sh", ".sql"]
Installers = [".msi", ".dmg", ".apk"]
Backups    = [".zip", ".7z", ".rar", ".tar", ".crypt8", ".crypt12"]
"""

_LAYOUT = """[layout]
movies_root      = "Video/Movies"
tv_root          = "Video/TV_Shows"
music_root       = "Audio/Music"
photos_root      = "Images/Photos"
personal_root    = "Video/Personal"
default_language = "Other"
language_folders = true

[layout.subtypes]
whatsapp     = "Video/WhatsApp"
anime        = "Video/Anime"
ads          = "Video/Ads_Promos"
sports       = "Video/Sports_Clips"
shorts       = "Video/Short_Clips"
audiobook    = "Audio/Audiobooks"
system_sound = "Audio/System_Sounds"
screenshot   = "Images/Screenshots"
graphic      = "Images/Graphics"
"""

_DEFAULT_JUNK_NAMES = ["Thumbs.db", "desktop.ini", ".DS_Store"]
_DEFAULT_JUNK_EXTS = [".tmp", ".crdownload", ".part"]
_DEFAULT_LANGS = {"English": ["english", "hollywood"]}


def _lit(path: str) -> str:
    """A TOML literal string for a path (backslashes survive verbatim)."""
    if "'" in path:
        raise ValueError(f"path may not contain a single quote: {path!r}")
    return "'" + path + "'"


def _basic(s: str) -> str:
    """A TOML basic string for a simple token (rejects an embedded quote)."""
    s = str(s)
    if '"' in s:
        raise ValueError(f"value may not contain a double quote: {s!r}")
    return '"' + s + '"'


def _arr(tokens) -> str:
    return "[" + ", ".join(_basic(t) for t in tokens) + "]"


def _toml_key(k: str) -> str:
    """A valid TOML key: bare if alnum/-/_, else a single-quoted LITERAL key
    (P21/A4) — backslashes and everything else survive verbatim, matching
    _lit's handling of path VALUES, and sidestepping basic-string escape
    rules entirely (a bare '\\NAS\\Share' is not valid TOML; a basic-quoted
    one would need every backslash escaped)."""
    if re.fullmatch(r"[A-Za-z0-9_-]+", k):
        return k
    if "'" in k:
        raise ValueError(f"staging key may not contain a single quote: {k!r}")
    return "'" + k + "'"


def _staging_for(library_root: str, sources: list[dict],
                 given: dict | None) -> dict[str, str]:
    """Per-root staging roots, unless the caller supplied an explicit map
    (P21/A4). A single Windows drive letter gets the legacy
    '<drive>:\\Delete-mlo'. Anything drive_of() can't identify as a single
    letter — a UNC share or a POSIX mount, where drive_of() is '' — is keyed
    by the root's OWN path with a 'Delete-mlo' staging dir appended under it,
    so the generated config is always loadable: never an invalid
    '\\\\server\\share:\\Delete' string, never a silently-empty [staging]."""
    if given:
        return given
    staging: dict[str, str] = {}
    for path in [library_root, *[s["root"] for s in sources]]:
        drive = winpath.drive_of(path)
        if drive and stagingmod.is_drive_letter_key(drive):
            key, root = drive, f"{drive}:\\Delete-mlo"
        elif drive:
            # UNC share: drive_of already returns the canonical whole-share
            # identity — key by it directly so root_for's exact-match fast
            # path resolves it (not just the longest-prefix fallback).
            key, root = drive, drive + "\\Delete-mlo"
        else:
            # POSIX mount, or anything drive_of can't identify: key by the
            # root's own path.
            key = path.rstrip("\\/")
            if not key:
                continue
            sep = "\\" if "\\" in path and "/" not in path else "/"
            if path == library_root:
                # Staging under the library root itself is refused by
                # config.validate (C4: staged files would be indexed as
                # library content) — place it BESIDE the root instead. The
                # key stays the root's own path, so root_for still resolves
                # everything under the library to this entry.
                root = key.rsplit(sep, 1)[0] + sep + "Delete-mlo"
            else:
                # Under a source root is allowed and normal (the scanners
                # prune staging dirs) and guarantees the same volume.
                root = key + sep + "Delete-mlo"
        if key not in staging:
            staging[key] = root
    return staging


def build_config_toml(answers: dict) -> str:
    """Render a valid mlo.toml from interview answers. Pure."""
    lib = answers["library_root"]
    sources = answers.get("sources", [])
    staging = _staging_for(lib, sources, answers.get("staging"))
    sacrosanct = answers.get("sacrosanct", [])          # protected substrings
    off_limits = answers.get("off_limits_drives", [])   # protected drives
    languages = answers.get("languages", _DEFAULT_LANGS)
    model = answers.get("local_model", "gpt-oss:20b")
    junk_names = answers.get("junk_names", _DEFAULT_JUNK_NAMES)
    junk_exts = answers.get("junk_extensions", _DEFAULT_JUNK_EXTS)

    parts = ["# mlo configuration — generated by `mlo init --interview`.\n"
             "# Every owner-specific answer lives here; the engine reads only this.\n"]
    parts.append(f"[library]\nroot = {_lit(lib)}\n")

    for s in sources:
        parts.append(f"[[sources]]\nname = {_basic(s['name'])}\n"
                     f"root = {_lit(s['root'])}\nenabled = true\n")

    parts.append("[staging]\n"
                 + "".join(f"{_toml_key(d)} = {_lit(p)}\n"
                          for d, p in staging.items()))

    parts.append(f"[protected]\nsubstrings = {_arr(sacrosanct)}\n"
                 f"drives = {_arr(off_limits)}\n")

    parts.append(f"[junk]\nzero_byte = true\nnames = {_arr(junk_names)}\n"
                 f"extensions = {_arr(junk_exts)}\n")

    parts.append("[classify]\nmax_unmatched_pct = 5.0\n")
    parts.append(_TAXONOMY)
    parts.append(_LAYOUT)
    # _toml_key/_basic on the free-text answers: a multi-word language name
    # ("Sri Lankan") or a quote in the model string would otherwise render
    # unloadable TOML — the C53 defect class (super-review M9).
    parts.append("[layout.languages]\n"
                 + "".join(f"{_toml_key(lang)} = {_arr(toks)}\n"
                          for lang, toks in languages.items()))
    parts.append(f'[llm]\nenabled = false\nchain = ["local"]\n\n'
                 f'[llm.local]\nenabled = false\n'
                 f'url = "http://localhost:11434"\nmodel = {_basic(model)}\n')

    return "\n".join(parts)


# ── the interactive driver ───────────────────────────────────────────────────

def _ask(input_fn, print_fn, prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    print_fn(prompt + suffix)
    ans = input_fn("> ").strip()
    return ans or default


def _ask_list(input_fn, print_fn, prompt: str, default: list) -> list:
    raw = _ask(input_fn, print_fn, prompt + " (comma-separated)",
               ", ".join(default))
    return [x.strip() for x in raw.split(",") if x.strip()]


def run_interview(input_fn=input, print_fn=print) -> dict:
    """Ask the config-surface questions; return an answers dict for
    build_config_toml. input_fn is injectable for tests."""
    print_fn("mlo onboarding — answer a few questions to generate mlo.toml.\n")
    library_root = _ask(input_fn, print_fn,
                        "Where should the clean, organized library live?")
    sources = []
    print_fn("\nWhich drives/folders hold the disorganized data to consolidate?")
    while True:
        root = _ask(input_fn, print_fn,
                    "Source root (blank to finish)")
        if not root:
            break
        name = _ask(input_fn, print_fn, "  a short name for this source",
                    f"src{len(sources) + 1}")
        sources.append({"name": name, "root": root})

    sacrosanct = _ask_list(
        input_fn, print_fn,
        "\nAny folders or apps that must NEVER be scanned/moved/deleted? "
        "(name fragments of a folder or app)", [])
    off_limits = _ask_list(
        input_fn, print_fn,
        "Which drive letters are entirely off-limits (not even read)?",
        ["C"])
    langs_raw = _ask_list(
        input_fn, print_fn,
        "\nWhich languages are in your collection?", ["English"])
    languages = {lang: [lang.lower()] for lang in langs_raw} or _DEFAULT_LANGS
    local_model = _ask(input_fn, print_fn,
                       "\nWhich local model runs the judgment tasks?",
                       "gpt-oss:20b")
    return {"library_root": library_root, "sources": sources,
            "sacrosanct": sacrosanct, "off_limits_drives": off_limits,
            "languages": languages, "local_model": local_model}
