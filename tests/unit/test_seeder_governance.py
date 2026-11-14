"""Seeder governance tests: nonsense filtering, query scoring, result cache."""

from __future__ import annotations

import httpx

from libref.crawler import UrlSeeder
from libref.crawler.url_seeder import _SEED_CACHE


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _llms_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path in ("/llms.txt", "/llms-full.txt"):
        return httpx.Response(
            200,
            text=(
                "- [A](https://docs.example.com/a.html)\n"
                "- [Map](https://docs.example.com/sitemap.xml)\n"
                "- [Icon](https://docs.example.com/favicon.ico)\n"
            ),
        )
    raise AssertionError(request.url.path)


def test_nonsense_urls_filtered(monkeypatch, tmp_path) -> None:
    _SEED_CACHE.clear()
    seeder = UrlSeeder(_client(_llms_handler), None, cache_ttl_hours=0)  # type: ignore[arg-type]
    result = _run(seeder.discover("https://docs.example.com/"))
    assert result.urls == ["https://docs.example.com/a.html"]


def test_query_scoring_orders_and_filters(monkeypatch, tmp_path) -> None:
    _SEED_CACHE.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/llms.txt", "/llms-full.txt"):
            return httpx.Response(
                200,
                text=(
                    "- [Team](https://docs.example.com/about/team.html)\n"
                    "- [Pip](https://docs.example.com/install/pip.html)\n"
                ),
            )
        raise AssertionError(request.url.path)

    seeder = UrlSeeder(_client(handler), None, cache_ttl_hours=0)  # type: ignore[arg-type]
    ranked = _run(seeder.discover("https://docs.example.com/", query="pip install"))
    assert ranked.urls[0] == "https://docs.example.com/install/pip.html"
    filtered = _run(
        seeder.discover(
            "https://docs.example.com/", query="pip install", score_threshold=0.9
        )
    )
    assert filtered.urls == ["https://docs.example.com/install/pip.html"]


def test_result_cache_avoids_refetch(monkeypatch, tmp_path) -> None:
    _SEED_CACHE.clear()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text="- [A](https://docs.example.com/a.html)\n")

    client = _client(handler)
    seeder = UrlSeeder(client, None, cache_ttl_hours=24)  # type: ignore[arg-type]
    first = _run(seeder.discover("https://docs.example.com/"))
    second = _run(seeder.discover("https://docs.example.com/"))
    assert first.urls == second.urls == ["https://docs.example.com/a.html"]
    # One fresh discovery reads every llms.txt variant once; the repeat reads none.
    assert sorted(calls) == ["/llms-full.txt", "/llms.txt"]
    third = _run(seeder.discover("https://docs.example.com/", force=True))
    assert third.urls == ["https://docs.example.com/a.html"]
    assert sorted(calls) == [
        "/llms-full.txt",
        "/llms-full.txt",
        "/llms.txt",
        "/llms.txt",
    ]
