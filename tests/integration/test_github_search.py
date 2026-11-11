"""GitHub explicit search tests: mapping, anonymous/token headers, errors, offline, cache."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from libref.crawler.registry import RegistryError
from libref.services.registry.discovery import DiscoveryService
from tests.conftest import make_discovery_settings

pytestmark = pytest.mark.integration

GITHUB_HTML_PAYLOAD = {
    "items": [
        {
            "full_name": "acme/demo",
            "description": "demo repo",
            "homepage": "https://demo.dev/",
            "html_url": "https://github.com/acme/demo",
            "stargazers_count": 42,
            "updated_at": "2026-05-01T00:00:00Z",
            "default_branch": "main",
        },
        {
            "full_name": "acme/helper",
            "description": "",
            "homepage": None,
            "html_url": "https://github.com/acme/helper",
            "stargazers_count": 1,
            "updated_at": "",
            "default_branch": "main",
        },
    ]
}


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "api.github.com":
        return httpx.Response(200, json=GITHUB_HTML_PAYLOAD)
    raise AssertionError(f"{request.url.host}{request.url.path}")


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


def test_search_github_maps_candidates_and_note(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run(svc.search_github_async("acme demo", 10, client()))
    assert len(items) == 2
    assert items[0]["ecosystem"] == "github"
    assert items[0]["name"] == "acme/demo"
    assert items[0]["repo_url"] == "https://github.com/acme/demo"
    assert items[0]["docs_url"] == "https://demo.dev/"
    assert items[0]["source"] == "search:github"
    assert "note" not in items[0]
    assert items[1]["repo_url"] == "https://github.com/acme/helper"
    assert items[1]["repo_url"] is not None
    assert items[1]["docs_url"] is None
    assert "note" not in items[1]


def test_search_github_repoless_note_when_no_repo(conn, monkeypatch, tmp_path) -> None:
    def norepo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "full_name": "acme/norepo",
                        "description": "no repo address",
                        "homepage": None,
                        "html_url": "not-a-url",
                        "stargazers_count": 0,
                        "updated_at": "",
                        "default_branch": "main",
                    }
                ]
            },
        )

    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    bad = httpx.AsyncClient(transport=httpx.MockTransport(norepo))
    items = run(svc.search_github_async("empty", 10, bad))
    assert len(items) == 1
    assert items[0]["repo_url"] is None
    assert "note" in items[0]


def test_search_github_anonymous_without_token(conn, monkeypatch, tmp_path) -> None:
    seen: dict = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=GITHUB_HTML_PAYLOAD)

    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    assert svc._settings is not None
    run(
        svc.search_github_async(
            "acme", 10, httpx.AsyncClient(transport=httpx.MockTransport(capture))
        )
    )
    assert seen.get("auth") is None


def test_search_github_bearer_with_token(conn, monkeypatch, tmp_path) -> None:
    seen: dict = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=GITHUB_HTML_PAYLOAD)

    svc = DiscoveryService(
        conn,
        make_discovery_settings(monkeypatch, tmp_path, discovery_github_token="tok"),
    )
    run(
        svc.search_github_async(
            "acme", 10, httpx.AsyncClient(transport=httpx.MockTransport(capture))
        )
    )
    assert seen.get("auth") == "Bearer tok"


def test_search_github_rate_limit_and_denied_are_retryable(
    conn, monkeypatch, tmp_path
) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))

    def limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "rate limited"})

    def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    with pytest.raises(RegistryError) as exc:
        run(
            svc.search_github_async(
                "demo", 10, httpx.AsyncClient(transport=httpx.MockTransport(limited))
            )
        )
    assert exc.value.kind == "retryable"
    with pytest.raises(RegistryError) as exc:
        run(
            svc.search_github_async(
                "demo", 10, httpx.AsyncClient(transport=httpx.MockTransport(server))
            )
        )
    assert exc.value.kind == "retryable"


def test_search_github_offline_uses_cache_or_fails(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    run(svc.search_github_async("acme demo", 10, client()))
    offline = DiscoveryService(
        conn, make_discovery_settings(monkeypatch, tmp_path, discovery_offline=True)
    )
    cached = run(offline.search_github_async("acme demo", 10, client()))
    assert len(cached) == 2
    with pytest.raises(RegistryError) as exc:
        run(offline.search_github_async("missing query xyz", 10, client()))
    assert exc.value.kind == "retryable"


def test_search_github_rejects_bad_input(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    with pytest.raises(ValueError):
        run(svc.search_github_async("  ", 10, client()))
    with pytest.raises(ValueError):
        run(svc.search_github_async("demo", 0, client()))
    disabled = DiscoveryService(
        conn, make_discovery_settings(monkeypatch, tmp_path, discovery_enabled=False)
    )
    with pytest.raises(ValueError):
        run(disabled.search_github_async("demo", 10, client()))


def test_search_github_cache_hit_makes_no_request(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    calls: list = []

    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return handler(request)

    first = run(
        svc.search_github_async(
            "acme demo",
            10,
            httpx.AsyncClient(transport=httpx.MockTransport(counting)),
        )
    )
    assert calls
    calls.clear()
    second = run(
        svc.search_github_async(
            "acme demo",
            10,
            httpx.AsyncClient(transport=httpx.MockTransport(counting)),
        )
    )
    assert calls == []
    assert second == first
