"""Enrichment connectors — mocked transports; fetch/parse/render logic and
clean degradation without a key or network. No connector writes a file."""
from __future__ import annotations

from mlo.enrich import id3, subs, tmdb, websearch


# ── TMDb ─────────────────────────────────────────────────────────────────────

def test_tmdb_search_parses_top_result():
    def fake(url, headers, timeout):
        assert "search/movie" in url
        assert headers["Authorization"].startswith("Bearer ")
        return {"results": [{"id": 27205, "title": "Inception",
                             "release_date": "2010-07-16",
                             "overview": "A thief.", "poster_path": "/x.jpg"}]}
    meta = tmdb.search_movie("Inception", 2010, key="K", transport=fake)
    assert meta["tmdb_id"] == 27205 and meta["year"] == 2010
    assert tmdb.poster_url(meta) == tmdb.IMG_BASE + "/x.jpg"


def test_tmdb_missing_key_is_clean_skip():
    # no key, no env var -> None, no crash, no network touched
    assert tmdb.search_movie("Inception", key=None, transport=None) is None


def test_tmdb_network_failure_degrades_to_none():
    def boom(url, headers, timeout):
        raise OSError("no net")
    assert tmdb.search_movie("X", key="K", transport=boom) is None


def test_tmdb_render_nfo_is_a_string_not_a_write():
    meta = {"tmdb_id": 1, "title": "Roja", "year": 1992, "overview": "..."}
    nfo = tmdb.render_nfo(meta)
    assert "<movie>" in nfo
    assert "<title>Roja</title>" in nfo and "<year>1992</year>" in nfo


# ── OpenSubtitles (P21/B5) ────────────────────────────────────────────────────

def test_opensubtitles_search_parses_results():
    def fake(url, headers, timeout):
        assert "subtitles?" in url and "query=" in url
        assert headers["Api-Key"] == "K"
        return {"data": [{"id": "123", "attributes": {
            "language": "en", "release": "Inception.2010.1080p",
            "files": [{"file_id": 456, "file_name": "inception.srt"}]}}]}
    out = subs.search_subtitles("Inception", 2010, key="K", transport=fake)
    assert out[0]["id"] == "123" and out[0]["file_id"] == 456
    assert out[0]["language"] == "en"


def test_opensubtitles_missing_key_is_clean_skip():
    assert subs.search_subtitles("Inception", key=None, transport=None) == []


def test_opensubtitles_empty_title_is_clean_skip():
    assert subs.search_subtitles("", key="K", transport=lambda *a: {}) == []


def test_opensubtitles_network_failure_degrades_to_empty_list():
    def boom(url, headers, timeout):
        raise OSError("no net")
    assert subs.search_subtitles("X", key="K", transport=boom) == []


def test_opensubtitles_no_results_key_yields_empty_list():
    assert subs.search_subtitles(
        "X", key="K", transport=lambda *a: {}) == []


def test_opensubtitles_result_with_no_files_has_none_file_id():
    def fake(url, headers, timeout):
        return {"data": [{"id": "1", "attributes": {"language": "en"}}]}
    out = subs.search_subtitles("X", key="K", transport=fake)
    assert out[0]["file_id"] is None and out[0]["file_name"] is None


# ── ID3 (mutagen optional) ───────────────────────────────────────────────────

def test_id3_read_is_total_without_a_valid_file(tmp_path):
    # missing path -> None whether or not mutagen is installed (offline-safe)
    assert id3.read_tags(str(tmp_path / "nope.mp3")) is None


# ── web search: batched + offline-safe ───────────────────────────────────────

def test_websearch_batches_queries():
    assert websearch.batch_queries(list("abcdef"), group_size=2) == \
        [["a", "b"], ["c", "d"], ["e", "f"]]


def test_websearch_offline_and_disabled_return_empty():
    live = lambda group, timeout: [{"x": 1}]           # noqa: E731
    assert websearch.search(["q"], enabled=False, transport=live) == []
    assert websearch.search([], enabled=True, transport=live) == []
    assert websearch.search(["q"], enabled=True, transport=None) == []


def test_websearch_enabled_batches_into_grouped_calls():
    calls: list[list[str]] = []

    def fake(group, timeout):
        calls.append(list(group))
        return [{"q": q} for q in group]
    res = websearch.search(["a", "b", "c"], enabled=True, transport=fake,
                           group_size=2)
    assert res == [{"q": "a"}, {"q": "b"}, {"q": "c"}]
    assert calls == [["a", "b"], ["c"]]                # 3 queries -> 2 requests
