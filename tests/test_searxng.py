"""SearXNG search adapter (P21/B1) — mocked transport, offline-safe."""
from __future__ import annotations

from mlo.enrich import searxng


def test_search_fn_parses_results_keyed_by_query(monkeypatch):
    def fake(url, headers, timeout):
        assert "format=json" in url
        assert "q=" in url
        return {"results": [{"title": "Inception", "url": "http://x",
                             "content": "a movie"}]}
    monkeypatch.setattr(searxng, "get_json", fake)
    fn = searxng.search_fn("http://localhost:8080")
    out = fn(["Inception movie"])
    assert out["Inception movie"][0]["title"] == "Inception"


def test_search_fn_returns_dict_keyed_by_every_query_in_the_group(monkeypatch):
    def fake(url, headers, timeout):
        return {"results": [{"title": "hit", "url": "u", "content": "c"}]}
    monkeypatch.setattr(searxng, "get_json", fake)
    fn = searxng.search_fn("http://localhost:8080")
    out = fn(["q1", "q2", "q3"])
    assert set(out) == {"q1", "q2", "q3"}
    assert all(len(v) == 1 for v in out.values())


def test_search_fn_degrades_to_empty_on_network_failure(monkeypatch):
    def boom(url, headers, timeout):
        raise OSError("no net")
    monkeypatch.setattr(searxng, "get_json", boom)
    fn = searxng.search_fn("http://localhost:8080")
    out = fn(["q1", "q2"])
    assert out == {"q1": [], "q2": []}


def test_search_fn_strips_trailing_slash_and_caps_results(monkeypatch):
    calls = []

    def fake(url, headers, timeout):
        calls.append(url)
        return {"results": [{"title": f"r{i}"} for i in range(10)]}
    monkeypatch.setattr(searxng, "get_json", fake)
    fn = searxng.search_fn("http://localhost:8080/", timeout=5)
    out = fn(["q"])
    assert calls[0].startswith("http://localhost:8080/search?")
    assert "//search" not in calls[0]
    assert len(out["q"]) == 5                      # capped at top 5


def test_search_fn_no_results_key_yields_empty_list(monkeypatch):
    monkeypatch.setattr(searxng, "get_json", lambda url, h, t: {})
    fn = searxng.search_fn("http://localhost:8080")
    assert fn(["q"]) == {"q": []}
