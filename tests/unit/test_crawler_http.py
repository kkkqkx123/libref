"""Crawler shared-client tests: proxy precedence, bypass mounting, test transport ignoring proxies."""

from __future__ import annotations

import asyncio

import httpx

from libref.config import LibrefSettings
from libref.crawler.http import (
    build_async_client,
    proxy_bypass_mounts,
    proxy_disabled,
    resolve_no_proxy_text,
    resolve_proxy_url,
)

PROXY_VARS = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


def _clear_proxy_env(monkeypatch) -> None:
    for var in PROXY_VARS:
        monkeypatch.delenv(var, raising=False)


def test_bypass_mounts_cover_loopback_and_suffix(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    mounts = proxy_bypass_mounts("localhost, 127.0.0.1, ::1, example.com, https://direct.example/x")
    assert mounts["all://localhost"] is None
    assert mounts["all://127.0.0.1"] is None
    assert mounts["all://[::1]"] is None
    assert mounts["all://*example.com"] is None
    assert mounts["https://direct.example/x"] is None


def test_bypass_mounts_skip_wildcard_and_empty(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    assert proxy_bypass_mounts("*, ") == {}
    assert proxy_disabled("*") is True
    assert proxy_disabled("localhost") is False


def test_explicit_proxy_wins_over_env(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://env-proxy:8080")
    monkeypatch.setenv("http_proxy", "http://env-proxy:8080")
    settings = LibrefSettings(http_proxy="http://explicit:9000")
    assert resolve_proxy_url(settings) == "http://explicit:9000"


def test_env_proxy_used_when_unset(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy:8080")
    monkeypatch.setenv("https_proxy", "http://env-proxy:8080")
    settings = LibrefSettings()
    assert resolve_proxy_url(settings) == "http://env-proxy:8080"


def test_no_proxy_unset_falls_back_to_loopback(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    assert resolve_no_proxy_text(LibrefSettings()) == "localhost,127.0.0.1,::1"


def test_no_proxy_explicit_wins_over_env(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("NO_PROXY", "env.example")
    monkeypatch.setenv("no_proxy", "env.example")
    settings = LibrefSettings(no_proxy="explicit.example")
    assert resolve_no_proxy_text(settings) == "explicit.example"


def test_transport_ignores_proxy(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    settings = LibrefSettings(http_proxy="http://explicit:9000")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    async def _run() -> str:
        client = build_async_client(settings, transport=httpx.MockTransport(handler))
        async with client:
            resp = await client.get("https://example.com/docs")
            return resp.text

    assert asyncio.run(_run()) == "ok"


def test_explicit_proxy_builds_without_network(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    settings = LibrefSettings(http_proxy="http://explicit:9000")

    async def _run() -> None:
        async with build_async_client(settings):
            pass

    asyncio.run(_run())
