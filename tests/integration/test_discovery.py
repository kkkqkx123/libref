"""Discovery service tests: MockTransport fan-out, ranking, cache, offline, gating."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from libref.crawler.registry import RegistryError
from libref.services.discovery import DiscoveryService
from tests.conftest import make_discovery_settings

pytestmark = pytest.mark.integration

NPM_SEARCH = {
    "objects": [
        {
            "package": {
                "name": "demo",
                "version": "3.0.0",
                "description": "demo package",
                "date": "2026-01-01T00:00:00.000Z",
                "links": {
                    "homepage": "https://demo.dev/",
                    "repository": "https://github.com/acme/demo",
                },
            },
            "score": {"final": 10.0, "detail": {}},
            "downloads": {"monthly": 500},
        },
        {
            "package": {
                "name": "demo-helper",
                "version": "1.0.0",
                "description": "helper",
                "links": {},
            },
            "score": {"final": 9.0, "detail": {}},
            "downloads": {"monthly": 900000},
        },
    ]
}

NPM_SNAPSHOT = {
    "dist-tags": {"latest": "3.0.0"},
    "versions": {"3.0.0": {}},
    "homepage": "https://demo.dev/",
    "repository": {"url": "https://github.com/acme/demo"},
}

MAVEN_SEARCH = {
    "response": {
        "docs": [
            {
                "g": "org.demo",
                "a": "demo",
                "latestVersion": "2.0",
                "timestamp": 1700000000000,
                "versionCount": 12,
            }
        ]
    }
}

MAVEN_METADATA = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<metadata>\n  <groupId>org.demo</groupId>\n  <artifactId>demo</artifactId>\n  <versioning>\n    <latest>2.0</latest>\n    <release>2.0</release>\n    <versions><version>1.0</version><version>2.0</version></versions>\n  </versioning>\n</metadata>\n"""

MAVEN_POM = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<project>\n  <url>https://demo.apache.org/</url>\n  <scm><url>scm:git:https://github.com/apache/demo.git</url></scm>\n</project>\n"""

PYPI_PAYLOAD = {
    "info": {
        "version": "9.9.9",
        "home_page": "https://demo.readthedocs.io/",
        "project_urls": {"Documentation": "https://demo.readthedocs.io/"},
    },
    "releases": {"9.9.9": []},
}

GITHUB_PAYLOAD = {
    "items": [
        {
            "full_name": "acme/demo",
            "description": "demo repo",
            "homepage": "https://demo.dev/",
            "html_url": "https://github.com/acme/demo",
            "stargazers_count": 42,
            "updated_at": "2026-05-01T00:00:00Z",
            "default_branch": "main",
        }
    ]
}


def handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    path = request.url.path
    if host == "registry.npmjs.org" and path == "/-/v1/search":
        return httpx.Response(200, json=NPM_SEARCH)
    if host == "registry.npmjs.org" and path == "/demo":
        return httpx.Response(200, json=NPM_SNAPSHOT)
    if host == "search.maven.org":
        return httpx.Response(200, json=MAVEN_SEARCH)
    if host == "repo1.maven.org" and path.endswith("maven-metadata.xml"):
        return httpx.Response(200, text=MAVEN_METADATA)
    if host == "repo1.maven.org" and path.endswith(".pom"):
        return httpx.Response(200, text=MAVEN_POM)
    if host == "pypi.org" and path == "/pypi/demo/json":
        return httpx.Response(200, json=PYPI_PAYLOAD)
    if host == "pypi.org":
        return httpx.Response(404, json={"message": "not found"})
    if host == "api.github.com":
        return httpx.Response(200, json=GITHUB_PAYLOAD)
    raise AssertionError(f"{host}{path}")


def counting_client(counter: list) -> httpx.AsyncClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        counter.append(str(request.url))
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped))


def run(coro):
    return asyncio.run(coro)


def test_npm_exact_enriched_and_ranked_first(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run(svc.search_async("demo", "npm", 10, counting_client([])))
    assert items[0]["name"] == "demo"
    assert items[0]["exact"] is True
    assert items[0]["docs_url"] == "https://demo.dev/"
    assert items[0]["source"] == "registry"
    assert items[1]["name"] == "demo-helper"
    assert items[1]["docs_url"] is None


def test_maven_exact_enriched_via_metadata(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run(svc.search_async("org.demo:demo", "maven", 10, counting_client([])))
    assert items[0]["name"] == "org.demo:demo"
    assert items[0]["exact"] is True
    assert items[0]["docs_url"] == "https://demo.apache.org/"
    assert items[0]["version"] == "2.0"


def test_pypi_exact_only(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run(svc.search_async("Demo", "pypi", 10, counting_client([])))
    assert len(items) == 1
    assert items[0]["name"] == "demo"
    assert items[0]["docs_url"] == "https://demo.readthedocs.io/"
    unknown = run(svc.search_async("no-such-pkg-xyz", "pypi", 10, counting_client([])))
    assert unknown == []


def test_cross_eco_fanout_and_github_gated(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(
        conn,
        make_discovery_settings(monkeypatch, tmp_path, discovery_github_token="tok"),
    )
    items = run(svc.search_async("demo", None, 10, counting_client([])))
    ecos = {c["ecosystem"] for c in items}
    assert {"npm", "maven", "pypi", "github"} <= ecos
    assert items[0]["docs_url"]


def test_github_skipped_without_token(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run(svc.search_async("demo", None, 10, counting_client([])))
    assert "github" not in {c["ecosystem"] for c in items}


def test_cache_hit_makes_no_request(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    calls: list = []
    first = run(svc.search_async("demo", "npm", 10, counting_client(calls)))
    assert calls
    calls.clear()
    second = run(svc.search_async("demo", "npm", 10, counting_client(calls)))
    assert calls == []
    assert second == first


def test_offline_cache_miss_is_retryable(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(
        conn, make_discovery_settings(monkeypatch, tmp_path, discovery_offline=True)
    )
    with pytest.raises(RegistryError, match="offline cache miss"):
        run(svc.search_async("demo", "npm", 10, counting_client([])))


def test_disabled_and_invalid_inputs(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(
        conn, make_discovery_settings(monkeypatch, tmp_path, discovery_enabled=False)
    )
    with pytest.raises(ValueError, match="disabled"):
        run(svc.search_async("demo", None, 10, counting_client([])))
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    with pytest.raises(ValueError, match="empty"):
        run(svc.search_async("  ", None, 10, counting_client([])))
    with pytest.raises(ValueError, match="unsupported ecosystem"):
        run(svc.search_async("demo", "cargo", 10, counting_client([])))
    with pytest.raises(ValueError, match="between 1 and 100"):
        run(svc.search_async("demo", None, 0, counting_client([])))


def test_ecosystem_aliases_share_canonical_cache(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    calls: list = []
    via_alias = run(svc.search_async("demo", "py", 10, counting_client(calls)))
    assert calls
    assert via_alias[0]["ecosystem"] == "pypi"
    calls.clear()
    via_canonical = run(svc.search_async("demo", "pypi", 10, counting_client(calls)))
    assert calls == []
    assert via_canonical == via_alias


def test_js_alias_matches_npm(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run(svc.search_async("demo", "js", 10, counting_client([])))
    assert items[0]["ecosystem"] == "npm"
    assert items[0]["name"] == "demo"


def test_pypi_fuzzy_search_lists_multiple_candidates(
    conn, monkeypatch, tmp_path
) -> None:
    import httpx

    html = (
        '<a class="package-snippet" href="/project/demo/">'
        '<span class="package-snippet__name">demo</span></a>'
        '<a class="package-snippet" href="/project/demo-helper/">'
        '<span class="package-snippet__name">demo-helper</span></a>'
    )

    def fuzzy_handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "pypi.org" and path == "/search":
            return httpx.Response(200, text=html)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(fuzzy_handler))
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run(svc.search_async("demo", "pypi", 10, client))
    assert [c["name"] for c in items] == ["demo", "demo-helper"]
    assert items[0]["exact"] is True
    assert items[0]["docs_url"] == "https://demo.readthedocs.io/"
    assert items[1]["source"] == "search:pypi"
