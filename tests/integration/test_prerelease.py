"""Prerelease support tests: version selection flips with the flag, caches stay isolated."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from libref.services.registry.discovery import DiscoveryService
from libref.services.registry.resolver import resolve_package
from tests.conftest import make_discovery_settings, make_registry_settings

pytestmark = pytest.mark.integration


def run(coro):
    return asyncio.run(coro)


NPM_SEARCH = {
    "objects": [
        {
            "package": {
                "name": "demo",
                "version": "2.0.0-beta",
                "description": "demo package",
                "date": "2026-01-01T00:00:00.000Z",
                "links": {"homepage": "https://demo.dev/"},
            },
            "score": {"final": 10.0, "detail": {}},
            "downloads": {"monthly": 100},
        }
    ]
}

NPM_SNAPSHOT = {
    "dist-tags": {"latest": "1.0.0"},
    "versions": {"1.0.0": {}, "2.0.0-beta": {}},
    "homepage": "https://demo.dev/",
    "repository": {"url": "https://github.com/acme/demo"},
}

CRATES_SNAPSHOT = {
    "crate": {
        "name": "demo",
        "description": "demo crate",
        "homepage": None,
        "repository": "https://github.com/acme/demo",
        "documentation": None,
        "max_stable_version": "1.0.0",
        "max_version": "2.0.0-beta",
    },
    "versions": [
        {"num": "1.0.0", "yanked": False},
        {"num": "2.0.0-beta", "yanked": False},
    ],
}


def handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    path = request.url.path
    if host == "registry.npmjs.org" and path == "/-/v1/search":
        return httpx.Response(200, json=NPM_SEARCH)
    if host == "registry.npmjs.org" and path == "/demo":
        return httpx.Response(200, json=NPM_SNAPSHOT)
    if host == "crates.io" and path == "/api/v1/crates/demo":
        return httpx.Response(200, json=CRATES_SNAPSHOT)
    raise AssertionError(f"{host}{path}")


def counting_client(counter: list) -> httpx.AsyncClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        counter.append(str(request.url))
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped))


def test_resolve_defaults_to_stable(conn, monkeypatch, tmp_path) -> None:
    settings = make_registry_settings(monkeypatch, tmp_path)
    res = run(resolve_package(conn, counting_client([]), settings, "npm", "demo"))
    assert res.version == "1.0.0"
    assert res.registry_confirmed is True


def test_resolve_flag_picks_prerelease_max(conn, monkeypatch, tmp_path) -> None:
    settings = make_registry_settings(monkeypatch, tmp_path)
    res = run(
        resolve_package(
            conn, counting_client([]), settings, "npm", "demo", include_prerelease=True
        )
    )
    assert res.version == "2.0.0-beta"
    assert res.registry_confirmed is True


def test_resolve_explicit_prerelease_needs_no_flag(conn, monkeypatch, tmp_path) -> None:
    settings = make_registry_settings(monkeypatch, tmp_path)
    res = run(
        resolve_package(
            conn, counting_client([]), settings, "npm", "demo", "2.0.0-beta"
        )
    )
    assert res.version == "2.0.0-beta"
    assert res.registry_confirmed is True


def test_flag_never_poisons_registry_cache(conn, monkeypatch, tmp_path) -> None:
    settings = make_registry_settings(monkeypatch, tmp_path)
    flagged = run(
        resolve_package(
            conn, counting_client([]), settings, "npm", "demo", include_prerelease=True
        )
    )
    assert flagged.version == "2.0.0-beta"
    row = conn.execute(
        "SELECT stable FROM registry_cache WHERE ecosystem='npm' AND name='demo'"
    ).fetchone()
    assert row is None or row[0] == "1.0.0"
    plain = run(resolve_package(conn, counting_client([]), settings, "npm", "demo"))
    assert plain.version == "1.0.0"


def test_flag_refetches_for_coherent_docs(conn, monkeypatch, tmp_path) -> None:
    settings = make_registry_settings(monkeypatch, tmp_path)
    plain = run(resolve_package(conn, counting_client([]), settings, "npm", "demo"))
    assert plain.version == "1.0.0"
    calls: list = []
    flagged = run(
        resolve_package(
            conn,
            counting_client(calls),
            settings,
            "npm",
            "demo",
            include_prerelease=True,
        )
    )
    assert flagged.version == "2.0.0-beta"
    assert calls, "flag path must refetch even with a fresh canonical cache"


def test_crates_flag_widens_stable(conn, monkeypatch, tmp_path) -> None:
    settings = make_registry_settings(monkeypatch, tmp_path)
    plain = run(resolve_package(conn, counting_client([]), settings, "crates", "demo"))
    assert plain.version == "1.0.0"
    assert plain.docs_url == "https://docs.rs/demo/1.0.0"
    flagged = run(
        resolve_package(
            conn,
            counting_client([]),
            settings,
            "crates",
            "demo",
            include_prerelease=True,
        )
    )
    assert flagged.version == "2.0.0-beta"
    assert flagged.docs_url == "https://docs.rs/demo/2.0.0-beta"


def test_discovery_flag_flips_enriched_version(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    plain = run(svc.search_async("demo", "npm", 10, counting_client([])))
    assert plain[0]["version"] == "1.0.0"
    flagged = run(
        svc.search_async(
            "demo", "npm", 10, counting_client([]), include_prerelease=True
        )
    )
    assert flagged[0]["version"] == "2.0.0-beta"
    assert flagged[0]["exact"] is True


def test_discovery_flag_cache_isolated(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    calls: list = []
    first = run(svc.search_async("demo", "npm", 10, counting_client(calls)))
    assert calls
    calls.clear()
    second = run(
        svc.search_async(
            "demo", "npm", 10, counting_client(calls), include_prerelease=True
        )
    )
    assert calls
    assert second[0]["version"] == "2.0.0-beta"
    assert first[0]["version"] == "1.0.0"
    calls.clear()
    third = run(svc.search_async("demo", "npm", 10, counting_client(calls)))
    assert calls == []
    assert third == first
