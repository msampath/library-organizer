"""All exported artifacts live here: plans (JSONL), summary.json, CSV views.

This is one of only two modules besides the kernel allowed to open files for
writing (test_architecture.py), and the ONLY one allowed to import csv — CSVs
are write-only views of the store (defect L7); nothing in the engine reads one.

Plans are the exception that proves the rule: they are hash-stamped artifacts
(the footer hash IS the plan_id) and apply re-verifies the hash before trusting
a single row (see docs/formats.md).
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import time

from . import __version__, winpath

PLAN_SCHEMA = "mlo.plan/1"
SUMMARY_SCHEMA = "mlo.summary/1"


class PlanIntegrityError(Exception):
    """Plan file corrupt or tampered — refuse to apply."""


def _runs_dir(workspace: str, run_id: str) -> str:
    d = os.path.join(workspace, "runs", run_id)
    os.makedirs(d, exist_ok=True)
    return d


def _plans_dir(workspace: str) -> str:
    d = os.path.join(workspace, "plans")
    os.makedirs(d, exist_ok=True)
    return d


# ── plans ────────────────────────────────────────────────────────────────────

def write_plan(workspace: str, kind: str, source_name: str, config_hash: str,
               inputs: list[dict], rows: list[dict]) -> tuple[str, str]:
    """Serialize a plan; returns (path, plan_id).

    plan_id is SEMANTIC identity — a hash over (kind, source, config, inputs,
    rows) that deliberately excludes the created timestamp, so an identical
    rebuild is the same plan regardless of when it was built (C3's
    executed-preservation depends on this; a timestamp in the id made it
    timing-dependent). The footer's content_sha256 is separate: byte-level
    FILE integrity over exactly what's on disk."""
    plan_id = hashlib.sha256(json.dumps(
        {"kind": kind, "source": source_name, "config_hash": config_hash,
         "inputs": inputs, "rows": rows},
        ensure_ascii=True, sort_keys=True).encode("utf-8")).hexdigest()
    header = {
        "schema": PLAN_SCHEMA,
        "kind": kind,
        "source": source_name,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "config_hash": config_hash,
        "inputs": inputs,
        "tool_version": __version__,
    }
    lines = [json.dumps(header, ensure_ascii=True, sort_keys=True)]
    lines += [json.dumps(r, ensure_ascii=True, sort_keys=True) for r in rows]
    body = ("\n".join(lines) + "\n").encode("utf-8", "surrogatepass")
    footer = json.dumps({"rows": len(rows), "plan_id": plan_id,
                         "content_sha256": hashlib.sha256(body).hexdigest()},
                        sort_keys=True)
    path = os.path.join(
        _plans_dir(workspace),
        f"plan-{kind}-{_safe_name(source_name)}-{plan_id[:8]}.jsonl")
    if os.path.exists(path):
        # Content-addressed: an identical rebuild IS the same artifact. Verify
        # integrity AND that the existing file's sealed plan_id matches the one
        # we just computed — an 8-hex filename collision between two different
        # plans would otherwise silently return the OLD file under the new id.
        _, _, existing_id = read_plan(path)
        if existing_id != plan_id:
            raise PlanIntegrityError(
                f"plan filename collision at {path}: existing plan_id "
                f"{existing_id[:12]}… != computed {plan_id[:12]}…")
        return path, plan_id
    with open(path, "xb") as f:
        f.write(body)
        f.write(footer.encode("utf-8"))
        f.write(b"\n")
    return path, plan_id


def read_plan(path: str) -> tuple[dict, list[dict], str]:
    """Parse + integrity-verify a plan. Returns (header, rows, plan_id)."""
    with open(winpath.to_long(path), "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8", "surrogatepass")
    lines = text.splitlines()
    if len(lines) < 2:
        raise PlanIntegrityError(f"not a plan file: {path}")
    try:
        footer = json.loads(lines[-1])
        header = json.loads(lines[0])
    except json.JSONDecodeError as e:
        raise PlanIntegrityError(f"unparseable plan {path}: {e}")
    if header.get("schema") != PLAN_SCHEMA:
        raise PlanIntegrityError(
            f"unknown plan schema {header.get('schema')!r} in {path}")
    body = ("\n".join(lines[:-1]) + "\n").encode("utf-8", "surrogatepass")
    digest = hashlib.sha256(body).hexdigest()
    if digest != footer.get("content_sha256"):
        raise PlanIntegrityError(
            f"plan hash mismatch in {path} — file corrupt or edited")
    rows = [json.loads(ln) for ln in lines[1:-1]]
    if len(rows) != footer.get("rows"):
        raise PlanIntegrityError(
            f"plan row count mismatch in {path} ({len(rows)} != {footer.get('rows')})")
    return header, rows, footer.get("plan_id", digest)


PROPOSAL_SCHEMA = "mlo.proposal/1"


def write_proposal(workspace: str, run_id: str, doc: dict) -> str:
    """The pilot's consolidated Pass-1 artifact: every section (plan + clusters
    + rehearsal counts), the critic review block, and the execution order — the
    single thing a human reviews. SEALED like a plan: proposal_sha256 over the
    canonical JSON (minus the seal), verified by read_proposal before any
    executor trusts it, so what was reviewed is provably what executes."""
    doc = {"schema": PROPOSAL_SCHEMA, "run": run_id, **doc}
    doc.pop("proposal_sha256", None)
    body = json.dumps(doc, sort_keys=True, ensure_ascii=True)
    doc["proposal_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    path = os.path.join(_runs_dir(workspace, run_id), "proposal.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.write("\n")
    return path


def read_proposal(path: str) -> dict:
    """Parse + integrity-verify a proposal. Raises PlanIntegrityError on any
    mismatch — a tampered or hand-edited proposal is never executed."""
    try:
        with open(winpath.to_long(path), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise PlanIntegrityError(f"unreadable proposal {path}: {e}")
    if doc.get("schema") != PROPOSAL_SCHEMA:
        raise PlanIntegrityError(
            f"unknown proposal schema {doc.get('schema')!r} in {path}")
    seal = doc.pop("proposal_sha256", None)
    body = json.dumps(doc, sort_keys=True, ensure_ascii=True)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if digest != seal:
        raise PlanIntegrityError(
            f"proposal hash mismatch in {path} — file corrupt or edited; "
            f"re-run `mlo pilot` and re-review")
    doc["proposal_sha256"] = seal
    return doc


# ── summary + views ──────────────────────────────────────────────────────────

def write_summary(workspace: str, run_id: str, summary: dict) -> str:
    summary = {"schema": SUMMARY_SCHEMA, "run": run_id, **summary}
    path = os.path.join(_runs_dir(workspace, run_id), "summary.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, sort_keys=True)
        f.write("\n")
    return path


def write_json(workspace: str, run_id: str, name: str, obj) -> str:
    """A machine-readable sidecar in the run directory (hints, unrouted lists).
    JSON, not CSV: these ARE meant to be read back by commands the user chains."""
    path = os.path.join(_runs_dir(workspace, run_id), f"{_safe_name(name)}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write("\n")
    return path


REVIEW_SET_SCHEMA = "mlo.review-set/1"


def write_review_set(workspace: str, run_id: str, items: list[dict]) -> str:
    """The engine->agents seam artifact (§3.3): the REVIEW residue, each item
    enriched with fingerprint, provenance and an ENUMERATED candidate-home menu,
    as JSONL. Self-contained so a Q4 model needs no 'go read 5 files'."""
    path = os.path.join(_runs_dir(workspace, run_id), "review-set.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"schema": REVIEW_SET_SCHEMA, "count": len(items)},
                           ensure_ascii=True, sort_keys=True) + "\n")
        for it in items:
            f.write(json.dumps(it, ensure_ascii=True, sort_keys=True) + "\n")
    return path


def write_run_text(workspace: str, run_id: str, filename: str, text: str) -> str:
    """A plain-text artifact in the run directory (e.g. a distilled-rules TOML
    snippet the human reviews before merging into mlo.toml). filename is
    code-controlled; the extension is preserved verbatim."""
    path = os.path.join(_runs_dir(workspace, run_id), filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def write_agent_ledger(workspace: str, run_id: str, entries: list[dict]) -> str:
    """The per-call LLM chain ledger (model, entry, outcome, latency, tokens) as
    a write-only JSONL view in the run directory — the audit trail agent-design.md
    §1 promises. Like the CSV exports (L7) nothing in the engine reads it back; it
    exists so chain behavior (which entry answered, how many fallback hops, how
    slow) stays inspectable after the fact."""
    path = os.path.join(_runs_dir(workspace, run_id), "agent-ledger.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=True, sort_keys=True) + "\n")
    return path


def summarize_ledger(entries: list[dict]) -> dict:
    """Roll the ledger up for a run summary / the eval table: how many calls
    answered, which entries carried them, average answer latency, fallback hops
    (a hop is any non-answering attempt at a chain entry)."""
    answered = [e for e in entries if e.get("outcome") == "ok" and "latency_s" in e]
    hops = sum(1 for e in entries
               if e.get("outcome") not in ("ok", None) and "latency_s" in e)
    by_entry: dict[str, int] = {}
    for e in answered:
        by_entry[e.get("entry", "?")] = by_entry.get(e.get("entry", "?"), 0) + 1
    lat = [e["latency_s"] for e in answered]
    return {
        "calls_answered": len(answered),
        "by_entry": by_entry,
        "fallback_hops": hops,
        "avg_latency_s": round(sum(lat) / len(lat), 3) if lat else None,
    }


def export_csv(workspace: str, run_id: str, name: str, fieldnames: list[str],
               rows, provenance: dict) -> str:
    """A grep-able view. First line is a comment row carrying provenance so a
    stray copy is self-identifying (and inert — nothing reads it back). `name`
    is sanitized (it may embed a source/table name): a truncating open() must
    never be steerable outside the run directory by a `..` traversal (defect
    C-sec: an arbitrary-.csv clobber primitive)."""
    path = os.path.join(_runs_dir(workspace, run_id), f"{_safe_name(name)}.csv")
    with open(path, "w", newline="", encoding="utf-8", errors="surrogatepass") as f:
        f.write("# " + json.dumps({"schema": "mlo.csv/1", **provenance},
                                  sort_keys=True) + "\n")
        # extrasaction='ignore': the fieldnames list IS the view's contract;
        # producers may carry extra (e.g. lossless) keys for other consumers.
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def _safe_name(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s) or "x"


STARTER_CONFIG = """\
# mlo configuration — the single source of truth (docs/architecture.md §9).
# Unknown keys are refused; dead drives get `enabled = false`, never comments.

[library]
root = 'X:\\Organized'            # where consolidated files live

[[sources]]
name = "old-drive"
root = 'E:\\'
enabled = true

# Per-drive staging roots. Staged files move here (same drive, reversible);
# mlo never deletes them — disposal is yours, deliberately.
[staging]
E = 'E:\\Delete'
X = 'X:\\Delete'

[protected]
substrings = ["bluestacks"]       # any path containing these is untouchable
drives = ["C"]

[junk]
zero_byte = true
names = ["Thumbs.db", "desktop.ini", ".DS_Store"]
extensions = [".tmp", ".crdownload", ".part"]

[classify]
max_unmatched_pct = 5.0           # above this, organize plans refuse to build

[taxonomy.buckets]
Video         = [".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".mpg", ".vob", ".3gp"]
Audio         = [".mp3", ".flac", ".m4a", ".wav", ".ogg", ".opus", ".amr", ".wma", ".3ga"]
Photos        = [".jpg", ".jpeg", ".png", ".gif", ".heic", ".webp",
                 ".raw", ".dng", ".kdc", ".cr2", ".nef", ".arw", ".orf", ".raf", ".rw2"]  # RAW: TIFF-based, EXIF-year readable
Documents     = [".pdf", ".docx", ".doc", ".txt", ".md"]
# These bucket LABELS are load-bearing: the C35 routes (Presentations ->
# Documents/Presentations, Archives/Installers shelf-drop), C37 bad-archive
# detection (checks the Archives bucket's extensions), and Comics/Ebooks
# series grouping all key on them. Renaming a label turns its mechanism off.
Presentations = [".ppt", ".pptx", ".odp", ".key"]
Spreadsheets  = [".xls", ".xlsx", ".ods"]
Comics        = [".cbz", ".cbr", ".cb7"]
# C43: "book" is an identity, not an extension — Books/<Last, First>/[Series/]
Ebooks        = [".epub", ".mobi", ".azw", ".azw3", ".azw4", ".prc", ".lit",
                 ".fb2", ".djvu"]
Code          = [".py", ".js", ".ts", ".cs", ".java", ".sh", ".sql"]
Installers    = [".msi", ".dmg", ".apk"]
Archives      = [".zip", ".7z", ".rar", ".tar"]
Backups       = [".crypt8", ".crypt12"]      # app-backup blobs, not archives

# Jellyfin-compatible placement is the DEFAULT (v0.2): media routes to
# content-derived groupings — Movies/Title (Year) under a language folder,
# Series/Season NN for TV, year folders for photos. A file whose identity
# can't be derived is never guessed: it falls back to <Bucket>/<source>/<path>
# (organize) or stays where it is (reorganize).
[layout]
movies_root      = "Video/Movies"
tv_root          = "Video/TV_Shows"
music_root       = "Audio/Music"
photos_root      = "Images/Photos"
personal_root    = "Video/Personal"
default_language = "Other"        # an explicit choice, not an implicit bucket
language_folders = true

[layout.languages]                # path/name tokens -> language folder
English   = ["english", "hollywood"]
Tamil     = ["tamil", "kollywood"]
Hindi     = ["hindi", "bollywood"]
Telugu    = ["telugu", "tollywood"]
Classical = ["classical", "carnatic", "instrumental"]

# Finer media types (§7): a critic-assigned media_kind -> a sub-root inside the
# library. The classifier/critic decides the kind (a WhatsApp forward vs a
# personal clip, anime vs a movie); the router just honours the mapping. Folder
# names live HERE, never in engine code (L6). Delete rows you do not want.
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

# Optional classifier conventions (uncomment to use). name_patterns feed the
# path classifier; audio_patterns extend the 'not all audio is music'
# pre-classifier (categories: devotional, comedy, voice, junk, ...).
#[classify.name_patterns]
#comedy = ['(?i)comedy']
#[classify.audio_patterns]
#comedy = ['(?i)\\bpattimandram\\b']

# Semantic containers (C33): subtrees that move as a UNIT (phone backups,
# drive images, app backups) have built-in patterns and homes; extend both
# together — a pattern kind with no home is a config error. (uncomment)
#[containers.patterns]
#dcim-roll = ['(?i)^dcim$']
#[containers.homes]
#dcim-roll = 'Backups/Phones'

# Opt-in enrichment connectors (P21/B1). Endpoints/toggles only — API keys
# are env vars / .mlo/.env (MLO_TMDB_KEY, MLO_OPENSUBTITLES_KEY), never here.
# [enrich]
# searxng_url = "http://localhost:8080"   # self-hosted SearXNG for --live-search
# tmdb_enabled = false
# id3_enabled = false
# opensubtitles_enabled = false

[llm]
enabled = false                   # agent layer opt-in (docs/agent-design.md)
chain = ["local"]
# critics_chain = ["claude-opus-4-8", "local"]   # stronger chain for critics only

[llm.local]
enabled = false
url = "http://localhost:11434"    # any OpenAI-compatible endpoint
model = "gpt-oss:20b"
"""


def write_starter_config(path: str) -> None:
    with open(path, "x", encoding="utf-8") as f:
        f.write(STARTER_CONFIG)


def write_config(path: str, text: str) -> None:
    """Write a generated mlo.toml (e.g. from `init --interview`). Never
    overwrites (open 'x') — the config write goes through this whitelisted
    module, not the CLI, so the kernel boundary holds."""
    with open(path, "x", encoding="utf-8") as f:
        f.write(text)


# ── UI-generated config ───────────────────────────────────────────────────────
# The web UI (mlo serve) authors a config from two folders the user picks. It is
# marked so the UI knows it is safe to overwrite (a hand-authored config is never
# clobbered — web.py checks for this marker before rewriting).
GENERATED_MARKER = "# mlo configuration generated by the web UI (mlo serve)"


def _toml_str(s: str) -> str:
    """A TOML basic string (double-quoted, backslashes/quotes escaped) — the one
    quoting that is safe for arbitrary Windows paths."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_generated_config(library_root: str, source_name: str,
                            source_root: str) -> str:
    """A minimal, valid config for the organize happy-path: one library, one
    source, the default taxonomy + Jellyfin layout. No [staging] — organize only
    copies UNIQUE files IN; disposal/dedup (which need staging) stay CLI-only."""
    return f"""\
{GENERATED_MARKER}
# Edit freely — or re-run the web UI to regenerate it.

[library]
root = {_toml_str(library_root)}

[[sources]]
name = {_toml_str(source_name)}
root = {_toml_str(source_root)}
enabled = true

[protected]
substrings = []                   # any path containing these is untouchable
drives = []                       # e.g. ["C"] to never touch the system drive

[junk]
zero_byte = true
names = ["Thumbs.db", "desktop.ini", ".DS_Store"]
extensions = [".tmp", ".crdownload", ".part"]

[classify]
max_unmatched_pct = 5.0           # above this, organize plans refuse to build

[taxonomy.buckets]
Video         = [".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".mpg", ".vob", ".3gp"]
Audio         = [".mp3", ".flac", ".m4a", ".wav", ".ogg", ".opus", ".amr", ".wma", ".3ga"]
Photos        = [".jpg", ".jpeg", ".png", ".gif", ".heic", ".webp", ".raw"]
Documents     = [".pdf", ".docx", ".doc", ".txt", ".md"]
Presentations = [".ppt", ".pptx", ".odp", ".key"]
Spreadsheets  = [".xls", ".xlsx", ".ods"]
Comics        = [".cbz", ".cbr", ".cb7"]
Ebooks        = [".epub", ".mobi", ".azw", ".azw3", ".azw4", ".prc", ".lit",
                 ".fb2", ".djvu"]
Code          = [".py", ".js", ".ts", ".cs", ".java", ".sh", ".sql"]
Installers    = [".msi", ".dmg", ".apk"]
Archives      = [".zip", ".7z", ".rar", ".tar"]
Backups       = [".crypt8", ".crypt12"]

[layout]
movies_root      = "Video/Movies"
tv_root          = "Video/TV_Shows"
music_root       = "Audio/Music"
photos_root      = "Images/Photos"
personal_root    = "Video/Personal"
default_language = "Other"
language_folders = true

[layout.languages]
English   = ["english", "hollywood"]
Tamil     = ["tamil", "kollywood"]
Hindi     = ["hindi", "bollywood"]
Telugu    = ["telugu", "tollywood"]
Classical = ["classical", "carnatic", "instrumental"]
"""


def write_generated_config(path: str, library_root: str, source_name: str,
                           source_root: str) -> None:
    """Write (overwriting) a UI-generated config. The caller (web.py) guarantees
    it is not clobbering a hand-authored file by checking GENERATED_MARKER."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_generated_config(library_root, source_name, source_root))
