"""New-ecosystem discovery tests: offline MockTransport fan-out, ranking, cache, and aliases."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from libref.crawler.registry import RegistryError
from libref.services.registry.discovery import DiscoveryService
from tests.conftest import make_discovery_settings

pytestmark = pytest.mark.integration


def run(coro):
    return asyncio.run(coro)


NPM_SEARCH = {
    "objects": [
        {
            "package": {
                "name": "demo-npm",
                "version": "1.0.0",
                "description": "npm demo",
                "date": "2026-01-01T00:00:00.000Z",
                "links": {"homepage": "https://demo.dev/"},
            },
            "score": {"final": 5.0, "detail": {}},
            "downloads": {"monthly": 10},
        }
    ]
}

MAVEN_SEARCH = {
    "response": {
        "docs": [
            {
                "g": "org.demo",
                "a": "demo-maven",
                "latestVersion": "1.0",
                "timestamp": 1700000000000,
                "versionCount": 3,
            }
        ]
    }
}

MAVEN_METADATA = """<?xml version="1.0" encoding="UTF-8"?>\n<metadata>\n  <versioning>\n    <release>1.0</release>\n    <versions><version>1.0</version></versions>\n  </versioning>\n</metadata>\n"""

MAVEN_POM = """<?xml version="1.0" encoding="UTF-8"?>\n<project>\n  <url>https://demo.apache.org/</url>\n</project>\n"""

PYPI_PAYLOAD = {
    "info": {
        "version": "1.0.0",
        "home_page": "https://demo.readthedocs.io/",
        "project_urls": {"Documentation": "https://demo.readthedocs.io/"},
    },
    "releases": {"1.0.0": []},
}

GO_SEARCH_HTML = (
    '<a href="/github.com/acme/demo">demo</a><a href="/github.com/acme/other">other</a>'
)

GO_LATEST = {"Version": "v1.2.3"}
GO_LIST = "v1.0.0\nv1.2.3\n"

CRATES_SEARCH = {
    "crates": [
        {
            "name": "demo",
            "description": "demo crate",
            "max_stable_version": "2.0.0",
            "homepage": None,
            "repository": "https://github.com/acme/demo",
            "documentation": None,
            "downloads": 500,
            "updated_at": "2026-01-01T00:00:00Z",
        }
    ]
}

CRATES_SNAPSHOT = {
    "crate": {
        "name": "demo",
        "description": "demo crate",
        "homepage": None,
        "repository": "https://github.com/acme/demo",
        "documentation": None,
        "max_stable_version": "2.0.0",
    },
    "versions": [{"num": "2.0.0", "yanked": False}],
}

NUGET_QUERY = {
    "data": [
        {
            "id": "Demo",
            "version": "2.0.0",
            "description": "demo package",
            "projectUrl": "https://demo.dev/",
            "totalDownloads": 9000,
        }
    ]
}

NUGET_REG = {
    "items": [
        {
            "items": [
                {
                    "catalogEntry": {
                        "version": "2.0.0",
                        "projectUrl": "https://demo.dev/",
                    }
                }
            ]
        }
    ]
}


def handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    path = request.url.path
    query = dict(request.url.params)
    if host == "registry.npmjs.org" and path == "/-/v1/search":
        return httpx.Response(200, json=NPM_SEARCH)
    if host == "search.maven.org":
        return httpx.Response(200, json=MAVEN_SEARCH)
    if host == "repo1.maven.org" and path.endswith("maven-metadata.xml"):
        return httpx.Response(200, text=MAVEN_METADATA)
    if host == "repo1.maven.org" and path.endswith(".pom"):
        return httpx.Response(200, text=MAVEN_POM)
    if host == "pypi.org" and path == "/pypi/demo/json":
        return httpx.Response(200, json=PYPI_PAYLOAD)
    if host == "pypi.org" and path == "/search":
        return httpx.Response(200, text="<html></html>")
    if host == "pypi.org":
        return httpx.Response(404, json={})
    if host == "pkg.go.dev" and path == "/search":
        if query.get("q") == "example.com/only-proxy":
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, text=GO_SEARCH_HTML)
    if host == "proxy.golang.org":
        if path == "/github.com/acme/demo/@latest":
            return httpx.Response(200, json=GO_LATEST)
        if path == "/github.com/acme/demo/@v/list":
            return httpx.Response(200, text=GO_LIST)
        if path == "/example.com/only-proxy/@latest":
            return httpx.Response(200, json={"Version": "v0.1.0"})
        if path == "/example.com/only-proxy/@v/list":
            return httpx.Response(200, text="v0.1.0\n")
        return httpx.Response(404, json={})
    if host == "crates.io" and path == "/api/v1/crates":
        return httpx.Response(200, json=CRATES_SEARCH)
    if host == "crates.io" and path == "/api/v1/crates/demo":
        return httpx.Response(200, json=CRATES_SNAPSHOT)
    if host == "azuresearch-usnc.nuget.org":
        return httpx.Response(200, json=NUGET_QUERY)
    if (
        host == "api.nuget.org"
        and path == "/v3/registration5-gz-semver2/demo/index.json"
    ):
        return httpx.Response(200, json=NUGET_REG)
    raise AssertionError(f"{host}{path}")


def counting_client(counter: list) -> httpx.AsyncClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        counter.append(str(request.url))
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped))


def test_go_search_enriched_via_proxy(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run(svc.search_async("github.com/acme/demo", "go", 10, counting_client([])))
    assert items[0]["name"] == "github.com/acme/demo"
    assert items[0]["exact"] is True
    assert items[0]["version"] == "v1.2.3"
    assert items[0]["docs_url"] == "https://pkg.go.dev/github.com/acme/demo@v1.2.3"
    assert items[0]["source"] == "registry"
    assert items[1]["name"] == "github.com/acme/other"


def test_go_exact_only_fallback(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run(
        svc.search_async("example.com/only-proxy", "go", 10, counting_client([]))
    )
    assert len(items) == 1
    assert items[0]["exact"] is True
    assert items[0]["version"] == "v0.1.0"


def test_crates_search_enriched(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run(svc.search_async("demo", "crates", 10, counting_client([])))
    assert items[0]["name"] == "demo"
    assert items[0]["exact"] is True
    assert items[0]["docs_url"] == "https://docs.rs/demo/2.0.0"
    assert items[0]["source"] == "registry"


def test_nuget_search_enriched(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run(svc.search_async("Demo", "nuget", 10, counting_client([])))
    assert items[0]["name"] == "Demo"
    assert items[0]["exact"] is True
    assert items[0]["docs_url"] == "https://demo.dev/"
    assert items[0]["source"] == "registry"


def test_cross_eco_fanout_covers_six(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run(svc.search_async("demo", None, 20, counting_client([])))
    assert {"npm", "maven", "pypi", "go", "crates", "nuget"} <= {
        c["ecosystem"] for c in items
    }


def test_new_eco_aliases_share_cache(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    calls: list = []
    via_alias = run(
        svc.search_async("github.com/acme/demo", "golang", 10, counting_client(calls))
    )
    assert calls
    assert via_alias[0]["ecosystem"] == "go"
    calls.clear()
    via_canonical = run(
        svc.search_async("github.com/acme/demo", "go", 10, counting_client(calls))
    )
    assert calls == []
    assert via_canonical == via_alias

    calls.clear()
    via_cargo = run(svc.search_async("demo", "cargo", 10, counting_client(calls)))
    assert calls
    assert via_cargo[0]["ecosystem"] == "crates"
    calls.clear()
    via_crates = run(svc.search_async("demo", "crates", 10, counting_client(calls)))
    assert calls == []
    assert via_crates == via_cargo


def test_new_eco_search_failure_is_retryable(conn, monkeypatch, tmp_path) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        if request.url.host == "crates.io":
            return httpx.Response(500, json={})
        return handler(request)

    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    with pytest.raises(RegistryError) as excinfo:
        run(
            svc.search_async(
                "demo",
                "crates",
                10,
                httpx.AsyncClient(transport=httpx.MockTransport(failing)),
            )
        )
    assert excinfo.value.kind == "retryable"
