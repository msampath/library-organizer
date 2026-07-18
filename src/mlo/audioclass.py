r"""Audio pre-classifier — deterministic 'not all audio is music' triage.

Before a title-only audio file is web-searched and handed to a music critic,
this classifies it BY NAME so the large fraction that isn't a song routes for
free (a real folder was 75% WhatsApp voice + 10% discourse, only ~14% songs):

  'voice'   WhatsApp / recorder voice notes (AUD-/PTT-...-WA, 'Recording NN')
            -> Audio\Personal
  'spoken'  lectures / discourses / podcasts — a numbered talk series with a
            huge id ('1006_1895639736_981_Telangana...', En-Pani-style)
            -> Audio\Audiobooks
  'junk'    macOS resource forks, hash-named caches, ringtone/test fixtures
  'song'    anything else carrying a real title -> the search + critic path
  None      no title at all (a numeric carve) -> stays for the human

First match wins; `extra` (from config, e.g. [classify.audio_patterns]) is
consulted before the built-ins so a user's convention overrides. The built-ins
are conventions distilled from a real ~340K-file library.
"""
from __future__ import annotations

import os
import re

# WhatsApp audio/push-to-talk and phone voice-recorder conventions.
_VOICE = (
    r"^(AUD|PTT)-\d{8}-WA\d",
    r"^Voice[ _-]*(Recorder|Memo|Note|Clip|\d)",
    r"^(New )?Recording[ _-]*\d",
    r"^REC[ _-]*\d",
    r"^WhatsApp (Audio|Ptt)",
)

# Spoken-word discourse/lecture series: a sequence number + a long numeric id.
_SPOKEN = (
    r"en[_ ]pani",
    r"^\d{1,4}_\d{6,}_",
)

_JUNK = (
    r"^\._",                    # macOS resource fork
    r"^[0-9a-f]{32}\b",         # hash-named cache/carve
    r"^sndhdr\b",               # python test fixtures
    r"^fallbackring\b",         # ringtone asset
)

_CLASSES = (("voice", _VOICE), ("spoken", _SPOKEN), ("junk", _JUNK))

# Devotional composition FORMS (pan-Indian, transliteration-tolerant). These name
# a devotional genre, not a deity, so they stay high-precision: 'bhajan', not
# 'rama'. A song matching one routes to Music\Devotional regardless of language.
# Substring match (transliteration-tolerant): these forms attach to a deity name
# as a suffix — GOVIND+ASHTAKAM, VISHNU+SAHASRANAMAM, LINGA+ASHTAKAM — so a
# leading word boundary would miss them.
#
# The base list (North-Indian / pan-Sanskrit vocabulary) came first; the second
# block (Tamil/Carnatic starter, C28) was added after a real-library dogfood
# where 40 Carnatic devotional recordings named 'thillana', 'paasuram', 'kriti',
# 'thevaram' etc. were slipping through as ordinary songs.
_DEVOTIONAL = (
    # ── pan-Sanskrit / Hindi vocabulary ──────────────────────────────────────
    r"bhajan",
    r"stotra",             # stotram / stotras
    r"ashtak",             # ashtakam / ashtaka
    r"sahasranam",         # sahasranamam
    r"chal[ie]+sa",        # chalisa / chaleesa
    r"kirtan",
    r"suprabhat",
    r"slok",               # slokam / sloka / slok
    r"namavali",
    r"abhang",
    r"tarangam",
    r"\baarti\b", r"\baarati\b", r"\barati\b",   # aarti forms (short -> bounded)
    r"\bstuti\b", r"\bstavam\b", r"\bgayatri\b", r"\bmantra\b",
    # ── Tamil Vaishnavite / Shaivite poetry (C28 starter) ────────────────────
    r"\bpaasuram(?:s)?\b", r"\bpasuram(?:s)?\b",         # Alwar/Andal poetry, singular
    r"\bpaasurangal\b", r"\bpasurangal\b",               # Tamil plural (-ngal)
    r"\bthevaram\b", r"\btevaram\b",                     # Nayanmar hymns
    r"\bthiruv(?:asag|asak|achag|achak)am\b",            # Manikkavachakar
    r"\btiruppavai\b", r"\bthiruppavai\b",               # Andal (double-p)
    r"\btirupavai\b", r"\bthirupavai\b",                 # Andal (single-p variant)
    r"\btirumurai\b", r"\bthirumurai\b",                 # Shaivite canon
    r"\bn[aā]{1,2}layira\b",                             # Naalayira
    r"divya\s*prabandham\b", r"\bprabandham\b", r"\bprabhandam\b",
    r"\bandal\b",
    r"\btaniyan\b", r"\bthaniyan\b",                     # invocatory verse
    # ── Carnatic composition forms ──────────────────────────────────────────
    r"\bkeerthana(?:m|s)?\b", r"\bkeertan(?:a|am)?\b",   # kīrtanam
    r"\bkritis?\b", r"\bkrithi\b",                       # kriti
    r"\bthillana\b", r"\btillana\b",                     # concluding piece
    r"\bvarna(?:m|s)?\b",                                # varnam
    r"\bswarajath?i\b",                                  # swarajathi/swarajati
    r"\bragamalika\b",
    # padam / javali intentionally omitted: Carnatic forms with heavy secular
    # (śṛṅgāra) use; 'Padam' also names Edith Piaf's French chanson. False
    # positives outweigh gains at auto-classify precision — user can add via
    # audioclass extension if their library needs them.
    # ── Sanskrit composition suffixes (deity + form) ────────────────────────
    r"\bashtapadi\b",
    r"\bsu?ktha?m\b", r"\bsooktham\b",                   # suktam / sooktham
    r"purushasuktam\b",
    r"\bpanchakshari\b",
    r"\brudram?\b",                                      # Sri Rudram
    r"\bashtottara(?:m|shatha?m?)?\b",
    r"\bmangalam\b",                                     # concluding benediction
    r"bhagav\w{0,3}\s+g[ie]{1,2}t[ah]?a?\b",             # Bhagavad/Bhagavath Gita/Githa
    r"gitopadesa\b", r"gitagovinda\b",
    # ── Dasa Sahitya (Kannada) / Annamacharya (Telugu) devarnama tradition ──
    r"\bdevarnama\b", r"\bdevaranama\b", r"\bdhevaranama\b",
    r"\bugabhoga\b",
    r"\bannamacharya\b", r"\bannamayya\b",               # composer-suffix ok
    r"\bsankirtana(?:m)?\b",                             # devotional chanting form
    # ── C29 addenda: named compositions & forms from the real dogfood ────────
    # Adi Shankara: "Bhaja Govindam". Very specific two-word phrase.
    r"\bbhaja\s+govind",
    # Nammalvar: Thiruvoimozhi (Tamil Vaishnavite). Trailing \w* accepts
    # numbered volumes ('Thiruvoimozhi2') — underscore/digit are word chars.
    r"\bthiruvo[iy]mozhi\w*", r"\btiruvo[iy]mozhi\w*",
    # Ramanuja: gadyam (Sharanagati/Sriranga/Sri Venkatesa gadyam).
    r"\bgadyam\b",
    # Annamacharya's most famous kriti "Nanati Bratuku"/"Nanati Baduku". Trailing
    # \w* accepts 'Nanati_Baduku'-style filenames (underscore is a word char).
    r"\bnanati\w*",
    # Purandaradasa devarnama "Sriman Narayana".
    r"\bsriman\s*narayana\b",
    # Sadasiva Brahmendra's iconic kriti (Manasa Sancharare / Sanchara / Sanchare).
    r"\bm[aā]{1,2}nasa\s+sanchar\w*",
    # Rajaji's famous devotional "Kurai Ondrum Illai" (to Krishna).
    r"\bkurai\s*ondrum\s*illai\b",
    # Deity-marriage compositions — form suffix that names a devotional genre.
    r"\bkalyana\w*",                                     # radha/padmavathi kalyanam
    r"\bparinay\w*",                                     # padmavathi/rukmini parinayam
    # Warkari sampradaya — Vittal/Vitthal bhakti.
    r"\bvitth?al\b",
    # Harikatha discourse-singer tradition (with a devotional-composition mix).
    r"\bharikatha\b", r"\bvi[sc]akah?\s*hari\b",         # 'Visaka hari' w/ or w/o space
    # Numbered Bhagavad Gita chapter recordings ("Gita 1", "Gita2", "Gita 047").
    # Bounded to Gita+number to avoid matching Gita as a proper name.
    r"\bgita\s*\d+\b",
    # Common chant phrases (multi-word — high precision).
    r"\bhare\s+murari\b", r"\bhare\s+krishna\b", r"\bhare\s+rama\b",
    r"\bgovinda\s+hare\b",
)

# A title with no real name — just a track index a ripper left behind. These
# have letters (so classify() called them a 'song') but nothing to place by.
_LOST = (
    r"^track\s*\d+\s*$",
    r"^file\d+\s*$",           # generic recovery-carve name (FILE002)
)


def classify(basename: str,
             extra: dict[str, tuple[str, ...]] | None = None) -> str | None:
    """Deterministic audio kind: 'voice' | 'spoken' | 'junk' | 'song' | None.
    Only 'song' proceeds to web search + the music critic; the rest are placed
    (or skipped) with zero model tokens."""
    name = basename.replace("\\", "/").rsplit("/", 1)[-1]
    for kind, patterns in list((extra or {}).items()) + list(_CLASSES):
        for p in patterns:
            if re.search(p, name, re.IGNORECASE):
                return kind
    stem = os.path.splitext(name)[0]
    if len(re.sub(r"[^A-Za-z]", "", stem)) >= 3:      # a real title -> a song
        return "song"
    return None


def song_bucket(basename: str,
                extra: dict[str, tuple[str, ...]] | None = None) -> str | None:
    """For a file already classified 'song', the finer Music sub-bucket:
    'devotional' (a bhajan/stotram/kriti — Music\\Devotional), 'lost' (a bare
    'Track NN' with no placeable title — Music\\Unsorted), or None (an ordinary
    song placed by language). Pure and name-only. `extra` is the user's
    [classify.audio_patterns] table — its 'devotional'/'lost' categories are
    consulted BEFORE the built-ins (the promised extension path for e.g.
    padam/javali libraries the built-in list deliberately omits)."""
    stem = os.path.splitext(basename.replace("\\", "/").rsplit("/", 1)[-1])[0]
    ex = extra or {}
    for kind, builtin in (("lost", _LOST), ("devotional", _DEVOTIONAL)):
        for p in tuple(ex.get(kind, ())) + tuple(builtin):
            if re.search(p, stem, re.IGNORECASE):
                return kind
    return None
