"""enrich.evidence — deterministic fact extraction + query composition + the
dedup/batch/cache assembly that keeps critic web-lookups token-cheap."""
from __future__ import annotations

from helpers import make_cfg
from mlo.enrich import evidence


def test_clean_name_url_decode_tags_and_disc_markers():
    assert evidence.clean_name(
        "Videos/x/Allen%20-%20Kadhal%20Sadugudu.flv") == "Allen - Kadhal Sadugudu"
    assert evidence.clean_name("x/[da-anime.info]DSQ - 01.mkv") == "DSQ - 01"
    assert evidence.clean_name("x/Bluffmaster.CD1.avi") == "Bluffmaster"
    assert evidence.clean_name("x/Break The Rules.mp3") == "Break The Rules"


def test_clean_name_strips_track_site_and_attribution():
    assert evidence.clean_name(
        "x/01 - Aaja Sanam - www.downloadming.com.mp3") == "Aaja Sanam"
    assert evidence.clean_name(
        "x/01. Tu Dayal Deen Haun -- Tulsidas.mp3") == "Tu Dayal Deen Haun"
    # a single dash keeps the raga/type structure; only a leading track # goes
    assert evidence.clean_name(
        "x/01 Viriboni - Varnam - Bhairavi.wma") == "Viriboni - Varnam - Bhairavi"
    # a bare single digit may be a real title ('3', the film) -> kept
    assert evidence.clean_name(
        "x/3 song (Idhazhin Oram) [Keep-Mp3.com].mp3") == "3 song (Idhazhin Oram)"


def test_compose_query_audio(world):
    cfg = make_cfg(world)
    item = {"relpath": "Audio/E_HDD2_Part1/Break The Rules.mp3",
            "bucket": "Audio", "language_guess": None}
    assert evidence.compose_query(item, cfg) == '"Break The Rules" song'


def test_compose_query_adds_language_when_known(world):
    cfg = make_cfg(world)
    item = {"relpath": "Audio/Tamil/Sakthi kodu.mp3", "bucket": "Audio",
            "language_guess": "Tamil"}
    q = evidence.compose_query(item, cfg)
    assert '"Sakthi kodu"' in q and "tamil" in q and "song" in q


def test_compose_query_none_for_numeric_carve(world):
    cfg = make_cfg(world)
    item = {"relpath": "Photos/x/190184408-190185158_001.PNG", "bucket": "Photos"}
    assert evidence.compose_query(item, cfg) is None


def test_compose_query_skips_non_song_audio(world):
    """The audio pre-classifier gates the search: a WhatsApp voice note and a
    discourse compose NO music query (routed deterministically), only a song."""
    cfg = make_cfg(world)
    voice = {"relpath": "Audio/I_SSD1/AUD-20150602-WA0001.wma",
             "bucket": "Audio"}
    spoken = {"relpath": "Audio/I_SSD1/947_1587962782_En_Pani_953.mp3",
              "bucket": "Audio"}
    song = {"relpath": "Audio/I_SSD1/Break The Rules.mp3", "bucket": "Audio"}
    assert evidence.compose_query(voice, cfg) is None
    assert evidence.compose_query(spoken, cfg) is None
    assert evidence.compose_query(song, cfg) == '"Break The Rules" song'


def test_assemble_dedups_batches_and_caches(world):
    cfg = make_cfg(world)
    items = [
        {"relpath": "Audio/a/Break The Rules.mp3", "bucket": "Audio"},
        {"relpath": "Audio/b/Break The Rules.mp3", "bucket": "Audio"},   # same query
        {"relpath": "Audio/c/Sakthi kodu.mp3", "bucket": "Audio"},
        {"relpath": "Photos/x/12345.png", "bucket": "Photos"},           # no query
    ]
    calls = []

    def fake_search(group):
        calls.append(list(group))
        return {q: [{"snippet": f"result:{q}"}] for q in group}

    roll = evidence.assemble(items, cfg, search_fn=fake_search)
    assert roll["queries"] == 3            # 3 items composed a query
    assert roll["searched"] == 2           # but only 2 UNIQUE queries were searched
    # the two identical-title items share one cached result (one lookup, not two)
    assert items[0]["evidence"]["web"] == items[1]["evidence"]["web"]
    assert "web_query" not in items[3].get("evidence", {})   # carve: no query
    assert len(calls) == 1                 # a single batched call-group


def test_assemble_cache_prevents_repeat_lookups(world):
    cfg = make_cfg(world)
    cache = {'"Break The Rules" song': [{"snippet": "cached"}]}
    items = [{"relpath": "Audio/z/Break The Rules.mp3", "bucket": "Audio"}]
    searched = []
    roll = evidence.assemble(items, cfg,
                             search_fn=lambda g: searched.append(g) or {},
                             cache=cache)
    assert roll["searched"] == 0 and roll["cache_hits"] == 1
    assert searched == []                  # never hit the network — served from cache
    assert items[0]["evidence"]["web"] == [{"snippet": "cached"}]
