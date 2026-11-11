"""Web search service tests: MockTransport mapping, offline, disabled, errors."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from libref.crawler.registry import RegistryError
from libref.services.registry.discovery import DiscoveryService
from tests.conftest import make_discovery_settings

pytestmark = pytest.mark.integration


DDG_HTML = (
    '<div class="result">'
    '<h2 class="result__title">'
    '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Facme%2Fdemo&amp;rut=x">acme demo</a>'
    "</h2>"
    '<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Facme%2Fdemo&amp;rut=x">demo snippet</a>'
    "</div>"
    '<div class="result">'
    '<a class="result__a" href="https://demo.readthedocs.io/">demo docs</a>'
    "</div>"
)


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "html.duckduckgo.com":
        return httpx.Response(200, text=DDG_HTML)
    raise AssertionError(f"{request.url.host}{request.url.path}")


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


def test_search_web_maps_repo_docs_and_note(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run(svc.search_web_async("acme demo", 10, client()))
    assert len(items) == 2
    assert items[0]["repo_url"] == "https://github.com/acme/demo"
    assert items[0]["name"] == "acme/demo"
    assert items[0]["source"] == "search:web"
    assert "note" not in items[0]
    assert items[1]["docs_url"] == "https://demo.readthedocs.io/"
    assert items[1]["repo_url"] is None
    assert "note" in items[1]


def test_search_web_offline_uses_cache_or_fails(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    run(svc.search_web_async("acme demo", 10, client()))
    offline = DiscoveryService(
        conn, make_discovery_settings(monkeypatch, tmp_path, discovery_offline=True)
    )
    cached = run(offline.search_web_async("acme demo", 10, client()))
    assert len(cached) == 2
    with pytest.raises(RegistryError) as exc:
        run(offline.search_web_async("missing query xyz", 10, client()))
    assert exc.value.kind == "retryable"


def test_search_web_rejects_bad_input(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    with pytest.raises(ValueError):
        run(svc.search_web_async("  ", 10, client()))
    with pytest.raises(ValueError):
        run(svc.search_web_async("demo", 0, client()))
    disabled = DiscoveryService(
        conn, make_discovery_settings(monkeypatch, tmp_path, discovery_enabled=False)
    )
    with pytest.raises(ValueError):
        run(disabled.search_web_async("demo", 10, client()))


def test_search_web_server_error_is_retryable(conn, monkeypatch, tmp_path) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="error")

    def challenged(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, text="challenge")

    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    bad = httpx.AsyncClient(transport=httpx.MockTransport(failing))
    with pytest.raises(RegistryError) as exc:
        run(svc.search_web_async("demo", 10, bad))
    assert exc.value.kind == "retryable"
    bot = httpx.AsyncClient(transport=httpx.MockTransport(challenged))
    with pytest.raises(RegistryError) as exc:
        run(svc.search_web_async("demo", 10, bot))
    assert exc.value.kind == "retryable"
