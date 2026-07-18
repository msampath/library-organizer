"""mediafacts — media metadata producers for the review-set (P21/B3):
id3 tags + TMDb candidates, injected-map style, matching hints.doc_props_map."""
from __future__ import annotations

import os

from mlo.enrich import mediafacts


# ── tags_map ─────────────────────────────────────────────────────────────────

def test_tags_map_includes_only_present_tags(tmp_path, monkeypatch):
    def fake_read_tags(path):
        if path.endswith("has_tags.mp3"):
            return {"artist": "Rahman", "title": "Song"}
        return None                      # no tags / unreadable / no mutagen
    monkeypatch.setattr(mediafacts.id3, "read_tags", fake_read_tags)
    rows = [{"relpath": "has_tags.mp3"}, {"relpath": "no_tags.mp3"}]
    out = mediafacts.tags_map(str(tmp_path), rows)
    assert out == {"has_tags.mp3": {"artist": "Rahman", "title": "Song"}}


def test_tags_map_accepts_plain_relpath_strings(tmp_path, monkeypatch):
    monkeypatch.setattr(mediafacts.id3, "read_tags",
                        lambda p: {"artist": "X"})
    out = mediafacts.tags_map(str(tmp_path), ["a.mp3"])
    assert out == {"a.mp3": {"artist": "X"}}


def test_tags_map_empty_rows_yields_empty_dict(tmp_path):
    assert mediafacts.tags_map(str(tmp_path), []) == {}


def test_tags_map_joins_root_and_relpath(tmp_path, monkeypatch):
    seen = []

    def fake_read_tags(path):
        seen.append(path)
        return None
    monkeypatch.setattr(mediafacts.id3, "read_tags", fake_read_tags)
    mediafacts.tags_map(str(tmp_path), [{"relpath": os.path.join("a", "b.mp3")}])
    assert seen == [os.path.join(str(tmp_path), "a", "b.mp3")]


# ── movie_candidates ─────────────────────────────────────────────────────────

def test_movie_candidates_only_considers_video_buckets():
    items = [
        {"relpath": "Video/Unsorted/Inception.2010.mkv", "bucket": "Video"},
        {"relpath": "Audio/Unsorted/song.mp3", "bucket": "Audio"},
    ]

    def fake_search(title, year=None, *, key=None, transport=None, timeout=20):
        return {"tmdb_id": 27205, "title": "Inception", "year": 2010}
    import mlo.enrich.mediafacts as mod
    orig = mod.tmdb.search_movie
    mod.tmdb.search_movie = fake_search
    try:
        out = mediafacts.movie_candidates(items, key="K")
    finally:
        mod.tmdb.search_movie = orig
    assert list(out) == ["Video/Unsorted/Inception.2010.mkv"]
    assert out["Video/Unsorted/Inception.2010.mkv"]["tmdb_id"] == 27205


def test_movie_candidates_absent_for_no_match(monkeypatch):
    items = [{"relpath": "Video/x.mkv", "bucket": "Video"}]
    monkeypatch.setattr(mediafacts.tmdb, "search_movie",
                        lambda *a, **k: None)
    out = mediafacts.movie_candidates(items, key="K")
    assert out == {}


def test_movie_candidates_accepts_videos_bucket_alias(monkeypatch):
    items = [{"relpath": "Video/x.mkv", "bucket": "Videos"}]
    monkeypatch.setattr(mediafacts.tmdb, "search_movie",
                        lambda *a, **k: {"tmdb_id": 1, "title": "X"})
    out = mediafacts.movie_candidates(items, key="K")
    assert "Video/x.mkv" in out


def test_movie_candidates_empty_items_yields_empty_dict():
    assert mediafacts.movie_candidates([], key="K") == {}
