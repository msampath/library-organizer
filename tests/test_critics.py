"""The classification critic panel (§4.1): per-language Movie/TV, Music, Photo,
adversarial tiebreak, and the dispatch + abstention ladder — all against the
scripted endpoint."""
from __future__ import annotations

import json

from mlo.agent import critics
from mlo.agent.llm import ChainClient
from test_agent_protocol import llm_cfg, scripted

LANGS = ("English", "Tamil", "Hindi", "Telugu", "Classical", "Other")


def _client(world, *replies):
    return ChainClient(llm_cfg(world), transport=scripted([*replies]))


# ── A3/A5: Movie/TV critic identifies and abstains ───────────────────────────

def test_movie_critic_identifies_and_abstains(world):
    spec = critics.movie_tv_critic_spec("English", LANGS)
    item = {"relpath": "x/Inception.mkv", "ext": ".mkv", "bucket": "Video",
            "candidate_homes": ["Video/Movies/English"]}
    good = {"media_kind": "movie", "language": "English", "year": 2010,
            "title": "Inception", "proposed_home": "Video/Movies/English",
            "confidence": 0.9, "rationale": "tmdb match"}
    v = critics.run_one(_client(world, json.dumps(good)), spec, item)
    assert v["media_kind"] == "movie" and v["year"] == 2010

    unsure = {"media_kind": "UNSURE", "language": "UNSURE", "year": None,
              "title": None, "proposed_home": None, "confidence": 0.2,
              "rationale": "cannot identify"}
    assert critics.run_one(_client(world, json.dumps(unsure)), spec, item) is None


# ── A4: Tamil critic is transliteration-aware ────────────────────────────────

def test_tamil_critic_is_transliteration_aware():
    tamil = critics.movie_tv_critic_spec("Tamil", LANGS)
    assert "TRANSLITERATED" in tamil.system and "fuzzy" in tamil.system.lower()
    english = critics.movie_tv_critic_spec("English", LANGS)
    assert "TRANSLITERATED" not in english.system      # no transliteration for English


def test_invented_language_never_validates(world):
    spec = critics.movie_tv_critic_spec("Tamil", LANGS)
    item = {"relpath": "x/a.mkv", "ext": ".mkv", "bucket": "Video",
            "candidate_homes": []}
    bad = {"media_kind": "movie", "language": "Klingon", "year": 2000,
           "title": "X", "proposed_home": "Video/Movies/Other",
           "confidence": 0.99, "rationale": "guess"}
    # invalid language fails validation through the repair loop -> abstains
    assert critics.run_one(_client(world, json.dumps(bad)), spec, item) is None


# ── A6: Music critic ─────────────────────────────────────────────────────────

def test_music_critic_uses_id3_evidence(world):
    spec = critics.music_critic_spec(LANGS)
    item = {"relpath": "Audio/x/song.mp3", "ext": ".mp3", "bucket": "Audio",
            "candidate_homes": ["Audio/Music/Tamil"]}
    reply = {"media_kind": "music", "language": "Tamil", "artist": "A R Rahman",
             "album": "Roja", "proposed_home": "Audio/Music/Tamil",
             "confidence": 0.88, "rationale": "id3"}
    v = critics.run_one(_client(world, json.dumps(reply)), spec, item,
                        evidence={"id3": {"artist": "A R Rahman"}})
    assert v["media_kind"] == "music" and v["artist"] == "A R Rahman"


# ── A7: Photo critic distinguishes photo / screenshot / graphic ──────────────

def test_photo_critic_maps_to_router_hints(world):
    spec = critics.photo_critic_spec()
    item = {"relpath": "Images/x/IMG.jpg", "ext": ".jpg", "bucket": "Images",
            "candidate_homes": []}
    photo = {"kind": "photo", "year": 2015, "device": "Pixel",
             "proposed_home": "Images/Photos/2015", "confidence": 0.9,
             "rationale": "exif"}
    v = critics.run_one(_client(world, json.dumps(photo)), spec, item)
    assert v["kind"] == "photo"
    assert critics._to_router_hint(v) == {"media_kind": None, "language": None,
                                          "year": 2015}
    # a screenshot maps to the finer media_kind (layout.subtypes)
    shot = {"kind": "screenshot", "year": None, "device": None,
            "proposed_home": "Images/Screenshots", "confidence": 0.9,
            "rationale": "no exif, web-sized"}
    assert critics._to_router_hint(shot) == {"media_kind": "screenshot",
                                             "language": None, "year": None}


# ── A8: adversarial tiebreak ─────────────────────────────────────────────────

def test_tiebreak_resolves_and_escalates(world):
    item = {"relpath": "Video/x/ambig.mkv", "bucket": "Video",
            "candidate_homes": []}
    a = {"media_kind": "movie", "proposed_home": "Video/Movies/Tamil",
         "confidence": 0.8}
    b = {"media_kind": "tv", "proposed_home": "Video/TV_Shows/English",
         "confidence": 0.75}
    won, rec = critics.resolve_tiebreak(
        _client(world, '{"winner": 0, "why": "stronger evidence"}'), item, [a, b])
    assert won is a and rec["winner"] == 0

    none, rec2 = critics.resolve_tiebreak(
        _client(world, '{"winner": "neither", "why": "both weak"}'), item, [a, b])
    assert none is None and rec2["resolution"] == "neither"


# ── A9: panel dispatch + abstention ladder + cross-check tiebreak ────────────

def test_panel_dispatches_by_bucket_and_abstains(world):
    cfg = llm_cfg(world)
    items = [
        {"relpath": "Video/eSrc/Roja.mkv", "ext": ".mkv", "bucket": "Video",
         "language_guess": "Tamil", "candidate_homes": []},
        {"relpath": "Audio/x/song.mp3", "ext": ".mp3", "bucket": "Audio",
         "language_guess": None, "candidate_homes": []},
        {"relpath": "Documents/x/notes.pdf", "ext": ".pdf",
         "bucket": "Documents", "candidate_homes": []},   # no critic -> unsure
    ]
    movie = {"media_kind": "movie", "language": "Tamil", "year": 1992,
             "title": "Roja", "proposed_home": "Video/Movies/Tamil",
             "confidence": 0.9, "rationale": "x"}
    music = {"media_kind": "music", "language": "Tamil", "artist": "A",
             "album": "B", "proposed_home": "Audio/Music/Tamil",
             "confidence": 0.85, "rationale": "x"}
    client = ChainClient(cfg, transport=scripted([json.dumps(movie),
                                                  json.dumps(music)]))
    out = critics.run_panel(client, cfg, items)
    assert out["hints"]["Video/eSrc/Roja.mkv"]["media_kind"] == "movie"
    assert out["hints"]["Audio/x/song.mp3"]["media_kind"] == "music"
    assert "Documents/x/notes.pdf" in out["unsure"]


def test_panel_cross_check_runs_tiebreak_on_disagreement(world):
    cfg = llm_cfg(world)
    item = {"relpath": "Video/eSrc/ambig.mkv", "ext": ".mkv", "bucket": "Video",
            "language_guess": "Tamil", "candidate_homes": []}
    tamil = {"media_kind": "movie", "language": "Tamil", "year": 1992,
             "title": "X", "proposed_home": "Video/Movies/Tamil",
             "confidence": 0.8, "rationale": "tamil critic"}
    intl = {"media_kind": "tv", "language": "Other", "year": None, "title": "X",
            "proposed_home": "Video/TV_Shows/Other", "confidence": 0.78,
            "rationale": "intl critic"}
    client = ChainClient(cfg, transport=scripted([
        json.dumps(tamil), json.dumps(intl),
        '{"winner": 0, "why": "tamil evidence stronger"}']))
    out = critics.run_panel(client, cfg, [item], cross_check=True)
    assert out["hints"]["Video/eSrc/ambig.mkv"]["media_kind"] == "movie"
    assert out["dissent"] and out["dissent"][0]["winner"] == 0


def test_panel_low_confidence_falls_through_to_unsure(world):
    cfg = llm_cfg(world)
    item = {"relpath": "Video/x/maybe.mkv", "ext": ".mkv", "bucket": "Video",
            "language_guess": "English", "candidate_homes": []}
    weak = {"media_kind": "movie", "language": "English", "year": None,
            "title": "Maybe", "proposed_home": "Video/Movies/English",
            "confidence": 0.5, "rationale": "not sure"}
    out = critics.run_panel(ChainClient(cfg, transport=scripted([json.dumps(weak)])),
                            cfg, [item])
    assert out["hints"] == {} and item["relpath"] in out["unsure"]


def test_panel_returns_full_answers_alongside_hints(world):
    """run_panel keeps the critic's FULL validated reply (proposed_home /
    confidence / rationale) in 'answers' — the review UI shows the reasoning,
    while 'hints' stays the narrowed router shape."""
    cfg = llm_cfg(world)
    items = [{"relpath": "Video/eSrc/Roja.mkv", "ext": ".mkv", "bucket": "Video",
              "language_guess": "Tamil", "candidate_homes": []}]
    movie = {"media_kind": "movie", "language": "Tamil", "year": 1992,
             "title": "Roja", "proposed_home": "Video/Movies/Tamil",
             "confidence": 0.9, "rationale": "well-known 1992 Mani Ratnam film"}
    client = ChainClient(cfg, transport=scripted([json.dumps(movie)]))
    out = critics.run_panel(client, cfg, items)
    ans = out["answers"]["Video/eSrc/Roja.mkv"]
    assert ans["proposed_home"] == "Video/Movies/Tamil"
    assert ans["rationale"].startswith("well-known")
    assert ans["confidence"] == 0.9
    # the router hint stays narrow (no rationale leak into the router surface)
    assert "rationale" not in out["hints"]["Video/eSrc/Roja.mkv"]


def test_cli_critics_chain_override(world, tmp_path, monkeypatch):
    """`agent critics --chain a,b` reaches the client as the overridden chain;
    with no --chain, [llm] critics_chain wins over [llm] chain. The kill-switch
    path is untouched (chain_config never enables)."""
    import mlo.agent.llm as llmmod
    from mlo.agent.llm import ChainExhausted
    from mlo.cli import main

    cfg_path = tmp_path / "mlo.toml"
    cfg_path.write_text(f'''
[library]
root = {str(world["lib"])!r}
[llm]
enabled = true
chain = ["local"]
critics_chain = ["claude-cfg", "local"]
''', encoding="utf-8")
    rs = tmp_path / "review-set.jsonl"
    rs.write_text(
        '{"schema": "mlo.review-set/1"}\n'
        '{"relpath": "Video/x/clip.mkv", "ext": ".mkv", "bucket": "Video",'
        ' "language_guess": null, "candidate_homes": []}\n', encoding="utf-8")

    captured = {}

    class FakeClient:
        def __init__(self, cfg, transport=None):
            captured["chain"] = cfg.llm.chain
        def complete(self, *a, **k):
            raise ChainExhausted("scripted: no entry answered")

    monkeypatch.setattr(llmmod, "ChainClient", FakeClient)
    monkeypatch.chdir(tmp_path)

    rc = main(["--config", str(cfg_path), "agent", "critics",
               "--review-set", str(rs), "--chain", "claude-flag,local"])
    assert rc == 0
    assert captured["chain"] == ("claude-flag", "local")    # --chain wins

    rc = main(["--config", str(cfg_path), "agent", "critics",
               "--review-set", str(rs)])
    assert rc == 0
    assert captured["chain"] == ("claude-cfg", "local")     # critics_chain next
