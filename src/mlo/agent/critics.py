"""The classification critic panel (§4.1) — the specialist judgment layer.

Each critic is a BOUNDED task over a single self-contained review-set item (the
seam artifact, §3.3): it reads what the engine already knows — fingerprint,
provenance, a language guess, an enumerated candidate-home menu — plus any
skill-derived evidence (a fuzzy-match shortlist, a TMDb hit, ID3 tags), and
returns a schema-validated hint or abstains (UNSURE). Critics never touch disk;
their hints INFORM a plan the human still gates.

The panel:
  - dispatches each item to its specialist by bucket + language — a per-language
    Movie/TV critic (Tamil/International are transliteration-aware and lean on
    the fuzzy skill), a Music critic, a Photo critic;
  - runs the ADVERSARIAL TIEBREAK when two critics disagree, recording the
    dissent;
  - applies the abstention + escalation ladder: run_task already climbs
    local->strong; a low-confidence or UNSURE answer falls through to the human
    (Unclassified), never a guess.

Everything the model may pick is enumerated from config/engine state (§2.8); a
critic can select a home or a language, never invent one.
"""
from __future__ import annotations

import json

from ..config import Config
from .llm import ChainClient
from .protocol import (SchemaError, TaskSpec, UNSURE, require_choice,
                       require_confidence, require_keys, run_task)

CONFIDENCE_FLOOR = 0.7
MARGINAL_BAND = 0.15        # a primary answer this close to the floor gets a cross-check

_MOVIE_KINDS = ("movie", "tv", "personal")
_PHOTO_KINDS = ("photo", "screenshot", "graphic")


def _year_ok(y) -> bool:
    return y is None or (isinstance(y, int) and not isinstance(y, bool)
                         and 1900 <= y <= 2035)


# ── critic specs ─────────────────────────────────────────────────────────────

def movie_tv_critic_spec(language: str, languages: tuple[str, ...]) -> TaskSpec:
    """A Movie/TV identity critic specialized to one language. Tamil and
    International get transliteration guidance and a nudge to use the fuzzy
    shortlist supplied as evidence."""
    langs = ", ".join(languages)
    translit = ""
    if language.lower() not in ("english", "other"):
        translit = (
            f" Titles are often TRANSLITERATED {language} (romanized), so "
            "spelling varies — prefer a candidate from the provided fuzzy "
            "shortlist over a fresh guess, and if two candidates fit equally, "
            "abstain rather than pick one. Abbreviations (KLTA, SMS-style) you "
            "cannot expand with confidence are UNSURE, not a guess.")

    def validate(obj: dict) -> dict:
        require_keys(obj, ("media_kind", "language", "year", "title",
                           "proposed_home", "confidence", "rationale"))
        require_choice(obj["media_kind"], set(_MOVIE_KINDS) | {UNSURE}, "media_kind")
        require_choice(obj["language"], set(languages) | {UNSURE}, "language")
        if not _year_ok(obj["year"]):
            raise SchemaError("year must be null or an integer 1900-2035")
        for k in ("title", "proposed_home", "rationale"):
            if obj[k] is not None and not isinstance(obj[k], str):
                raise SchemaError(f"{k} must be a string or null")
        require_confidence(obj)
        return obj

    return TaskSpec(
        name=f"critic-movie-{language.lower()}",
        system=(
            f"You are a {language} film and television critic placing ONE file "
            "in a personal library. Judge from the path, the provenance origin, "
            "and any TMDb/fuzzy evidence given. Decide media_kind — 'movie', "
            "'tv', or 'personal' (home/camera/event footage), or UNSURE; give "
            f"language (one of {langs}, or UNSURE), year (integer 1900-2035 or "
            "null), a clean title, and proposed_home chosen from the candidate "
            f"homes provided.{translit} Never invent a year or a title you "
            "cannot support. When torn, UNSURE beats a guess — a wrong "
            "confident answer costs more than an abstention. Reply with ONLY a "
            'JSON object: {"media_kind","language","year","title",'
            '"proposed_home","confidence","rationale"}.'),
        validate=validate)


def music_critic_spec(languages: tuple[str, ...]) -> TaskSpec:
    langs = ", ".join(languages)

    def validate(obj: dict) -> dict:
        require_keys(obj, ("media_kind", "language", "artist", "album",
                           "proposed_home", "confidence", "rationale"))
        require_choice(obj["media_kind"], {"music", "personal", UNSURE},
                       "media_kind")
        require_choice(obj["language"], set(languages) | {UNSURE}, "language")
        for k in ("artist", "album", "proposed_home", "rationale"):
            if obj[k] is not None and not isinstance(obj[k], str):
                raise SchemaError(f"{k} must be a string or null")
        require_confidence(obj)
        return obj

    return TaskSpec(
        name="critic-music",
        system=(
            "You are a music librarian placing ONE audio file. Use the path, "
            "provenance, any ID3 tags, the web-search evidence, and the "
            "siblings_in_folder. Decide media_kind — 'music' or 'personal' (a "
            f"voice note/recording), or UNSURE; give language (one of {langs}, "
            "or UNSURE), artist, album, and proposed_home from the candidate "
            "homes. CRITICAL: judge THIS file's language from ITS OWN song "
            "identity — a folder is often a MIXED-language compilation (a Tamil "
            "film song sitting beside Hindi ones), so the siblings tell you the "
            "DOMAIN (e.g. Indian film music) but NOT this file's language. Do "
            "not assume the folder has one language. A JDK/system beep or app UI "
            "sound is NOT music — abstain (UNSURE) if you cannot tell. Reply "
            'with ONLY a JSON object: {"media_kind","language","artist",'
            '"album","proposed_home","confidence","rationale"}.'),
        validate=validate)


def photo_critic_spec() -> TaskSpec:
    def validate(obj: dict) -> dict:
        require_keys(obj, ("kind", "year", "device", "proposed_home",
                           "confidence", "rationale"))
        require_choice(obj["kind"], set(_PHOTO_KINDS) | {UNSURE}, "kind")
        if not _year_ok(obj["year"]):
            raise SchemaError("year must be null or an integer 1900-2035")
        for k in ("device", "proposed_home", "rationale"):
            if obj[k] is not None and not isinstance(obj[k], str):
                raise SchemaError(f"{k} must be a string or null")
        require_confidence(obj)
        return obj

    return TaskSpec(
        name="critic-photo",
        system=(
            "You are a photo archivist placing ONE image file. Use EXIF facts, "
            "provenance, size, and the path. Decide kind — 'photo' (a real "
            "camera/phone photograph), 'screenshot', or 'graphic' (icon, "
            "sticker, web asset, meme), or UNSURE. For a real photo give the "
            "year (from EXIF, integer or null) and device if known. A tiny "
            "web-sized image with no EXIF is usually a graphic; a real "
            "photograph carries EXIF and camera dimensions. proposed_home comes "
            "from the candidate homes. Reply with ONLY a JSON object: "
            '{"kind","year","device","proposed_home","confidence","rationale"}.'),
        validate=validate)


def tiebreak_spec(n_options: int) -> TaskSpec:
    def validate(obj: dict) -> dict:
        require_keys(obj, ("winner", "why"))
        w = obj["winner"]
        if not (isinstance(w, int) and not isinstance(w, bool)
                and 0 <= w < n_options) and w != "neither":
            raise SchemaError(
                f"winner must be an integer 0..{n_options - 1} or 'neither'")
        return obj

    return TaskSpec(
        name="critic-tiebreak",
        system=(
            "Two specialist critics disagree about ONE file. Weigh their "
            "evidence and pick the best-supported answer by its index, or "
            "'neither' if neither is defensible (the file then goes to a "
            'human). Reply with ONLY: {"winner": <index or "neither">, '
            '"why": "<short>"}.'),
        validate=validate)


# ── prompt rendering ─────────────────────────────────────────────────────────

def _render_item(item: dict, evidence: dict) -> str:
    view = {
        "path": item.get("relpath"),
        "ext": item.get("ext"),
        "size": item.get("size"),
        "bucket": item.get("bucket"),
        "language_guess": item.get("language_guess"),
        "origin": item.get("origin"),
        "origin_signal": item.get("origin_signal"),
        "candidate_homes": item.get("candidate_homes"),
    }
    if item.get("siblings"):
        view["siblings_in_folder"] = item["siblings"]   # the neighbourhood — context
    if item.get("mtime"):
        view["mtime"] = item["mtime"]
    if item.get("doc_props"):
        # Embedded document properties (creator/title/company/dates) — what a
        # human reads after OPENING the file. CANONICAL rule (owner, 2026-07-09):
        # a critic judges with ALL of these signals, never the filename alone.
        view["doc_props"] = item["doc_props"]
    if item.get("media_tags"):
        # P21/B3: real embedded ID3 tags (artist/album/title/genre/date) —
        # closes the gap where the music-critic prompt asked for "any ID3
        # tags" that the pipeline never actually supplied.
        view["media_tags"] = item["media_tags"]
    if item.get("title_candidates"):
        # P21/B3: a real TMDb match for the cleaned filename title — closes
        # the same gap for "TMDb evidence" on the movie critic.
        view["title_candidates"] = item["title_candidates"]
    if evidence:
        view["evidence"] = evidence           # fuzzy shortlist, tmdb, id3, exif
    return json.dumps(view, ensure_ascii=True, sort_keys=True)


# ── running critics and the panel ────────────────────────────────────────────

def run_one(client: ChainClient, spec: TaskSpec, item: dict,
            evidence: dict | None = None) -> dict | None:
    """Run one critic on one item; None on abstention (UNSURE / no valid reply)."""
    out = run_task(client, spec, _render_item(item, evidence or {}))
    if out.value is None:
        return None
    v = out.value
    kind = v.get("media_kind", v.get("kind"))
    if kind == UNSURE:
        return None
    return v


def resolve_tiebreak(client: ChainClient, item: dict,
                     competing: list[dict]) -> tuple[dict | None, dict]:
    """Adversarial resolution of two disagreeing hints. Returns (winner-or-None,
    dissent record). 'neither' -> None (escalate to human), dissent logged."""
    user = (_render_item(item, {})
            + "\n\nCompeting answers:\n"
            + "\n".join(f"{i}: {json.dumps(c, sort_keys=True)}"
                        for i, c in enumerate(competing)))
    out = run_task(client, tiebreak_spec(len(competing)), user)
    if out.value is None or out.value["winner"] == "neither":
        return None, {"item": item.get("relpath"), "competing": competing,
                      "resolution": "neither" if out.value else "unresolved"}
    win = out.value["winner"]
    return competing[win], {"item": item.get("relpath"), "competing": competing,
                            "winner": win, "why": out.value.get("why")}


def _to_router_hint(v: dict) -> dict:
    """Map a critic answer to a router Hints-shaped dict (media_kind/language/
    year). A real photo carries only a year (the router's photo path handles it);
    screenshot/graphic become finer media_kinds (layout.subtypes)."""
    if "kind" in v and "media_kind" not in v:          # photo critic
        if v["kind"] == "photo":
            return {"media_kind": None, "language": None, "year": v.get("year")}
        return {"media_kind": v["kind"], "language": None, "year": v.get("year")}
    lang = v.get("language")
    return {"media_kind": v.get("media_kind"),
            "language": None if lang in (None, UNSURE) else lang,
            "year": v.get("year")}


def _critics_for(cfg: Config, item: dict, languages: tuple[str, ...],
                 cross_check: bool) -> list[TaskSpec]:
    bucket = item.get("bucket")
    if bucket in ("Video", "Videos"):
        primary = item.get("language_guess") or cfg.layout.default_language
        specs = [movie_tv_critic_spec(primary, languages)]
        if cross_check and primary != cfg.layout.default_language:
            specs.append(movie_tv_critic_spec(cfg.layout.default_language,
                                              languages))
        return specs
    if bucket == "Audio":
        return [music_critic_spec(languages)]
    if bucket in ("Photos", "Images"):
        return [photo_critic_spec()]
    if bucket == "Ebooks":
        # C43: out-of-engine judgment by design — title-only book identity
        # (famous-work knowledge) is handled by an Opus 4.8 subagent batch
        # over the review-set (docs/roadmap.md book_critic_spec row), not an
        # in-engine chain critic. No spec here means the panel abstains
        # (UNSURE) and the item stays in the human/subagent queue.
        return []
    return []


def _agree(a: dict, b: dict) -> bool:
    ak = a.get("media_kind", a.get("kind"))
    bk = b.get("media_kind", b.get("kind"))
    return ak == bk and a.get("proposed_home") == b.get("proposed_home")


def run_panel(client: ChainClient, cfg: Config, items: list[dict], *,
              evidence: dict | None = None,
              confidence_floor: float = CONFIDENCE_FLOOR,
              cross_check: bool = False) -> dict:
    """Route each review item to its specialist(s), resolve disagreements with
    the tiebreak, and apply the abstention ladder. Returns router-ready hints,
    the abstained (Unclassified) list, and the dissent log."""
    evidence = evidence or {}
    languages = tuple(dict.fromkeys(
        list(cfg.layout.languages) + [cfg.layout.default_language]))
    hints: dict[str, dict] = {}
    resolved_answers: dict[str, dict] = {}
    unsure: list[str] = []
    dissent: list[dict] = []

    for item in items:
        rel = item["relpath"]
        ev = evidence.get(rel, {})
        specs = _critics_for(cfg, item, languages, cross_check)
        if not specs:
            unsure.append(rel)
            continue
        answers = [a for a in (run_one(client, s, item, ev) for s in specs)
                   if a is not None]
        if not answers:
            unsure.append(rel)
            continue
        if len(answers) == 1 or _agree(answers[0], answers[1]):
            resolved = max(answers, key=lambda a: a.get("confidence", 0))
        else:
            resolved, rec = resolve_tiebreak(client, item, answers[:2])
            dissent.append(rec)
        if resolved is None or resolved.get("confidence", 0) < confidence_floor:
            unsure.append(rel)
        else:
            hints[rel] = _to_router_hint(resolved)
            # The FULL validated reply (proposed_home/rationale/confidence)
            # survives alongside the narrowed router hint — the review UI
            # shows the critic's reasoning, not just the verdict.
            resolved_answers[rel] = resolved
    return {"hints": hints, "answers": resolved_answers,
            "unsure": unsure, "dissent": dissent}
