"""Fetcher tests: domain penalty on rate limiting, cache-mode bypass."""

from __future__ import annotations

import asyncio

import httpx

from libref.crawler.fetcher import HttpFetcher


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_penalty_applies_after_rate_limit(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, text="ok")

    async def _run() -> None:
        async with _client(handler) as client:
            fetcher = HttpFetcher(client, max_retries=1, default_delay=0.0)
            first = await fetcher.fetch("https://d.example/a.html")
            assert first.status == "ok"
            assert fetcher.penalized_hosts() == []

    asyncio.run(_run())
    assert len(calls) == 2


def test_repeated_rate_limit_penalizes_host(monkeypatch, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    async def _run() -> list[str]:
        async with _client(handler) as client:
            fetcher = HttpFetcher(client, max_retries=0, default_delay=0.0)
            outcome = await fetcher.fetch("https://d.example/a.html")
            assert outcome.status == "failed"
            return fetcher.penalized_hosts()

    assert asyncio.run(_run()) == ["d.example"]


def test_bypass_skips_conditional_headers(monkeypatch, tmp_path) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, text="ok")

    async def _run() -> None:
        async with _client(handler) as client:
            fetcher = HttpFetcher(client, max_retries=0, default_delay=0.0)
            await fetcher.fetch(
                "https://d.example/a.html", etag="abc", cache_mode="bypass"
            )
            assert "if-none-match" not in seen
            await fetcher.fetch(
                "https://d.example/a.html", etag="abc", cache_mode="enabled"
            )
            assert seen.get("if-none-match") == "abc"

    asyncio.run(_run())


def test_declared_body_over_budget_not_retried() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="ok", headers={"content-length": "100"})

    async def _run() -> None:
        async with _client(handler) as client:
            fetcher = HttpFetcher(client, max_retries=2, max_body_bytes=10)
            outcome = await fetcher.fetch("https://d.example/a.html")
            assert outcome.status == "failed"
            assert "budget" in (outcome.error or "")

    asyncio.run(_run())
    assert len(calls) == 1


def test_actual_body_over_budget() -> None:
    from libref.crawler.fetcher import HttpFetcher as _Fetcher

    async def _run() -> None:
        resp = httpx.Response(200, content=b"x" * 20)
        del resp.headers["content-length"]
        fetcher = _Fetcher.__new__(_Fetcher)
        fetcher._max_body_bytes = 10
        text, over = await fetcher._read_capped(resp)
        assert text is None
        assert over is not None and "budget" in over
        resp = httpx.Response(200, content=b"ok")
        del resp.headers["content-length"]
        text, over = await fetcher._read_capped(resp)
        assert text == "ok"
        assert over is None

    asyncio.run(_run())
