"""Discovery service tests: MockTransport fan-out, ranking, cache, offline, gating."""

from __future__ import annotations

import httpx
import pytest

from libref.crawler.registry import RegistryError
from libref.services.registry.discovery import DiscoveryService
from tests.conftest import counting_client, make_discovery_settings, run_coro

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
    if host == "pypi.org" and path == "/search":
        return httpx.Response(200, text="<html></html>")
    if host == "pypi.org":
        return httpx.Response(404, json={"message": "not found"})
    if host == "api.github.com":
        return httpx.Response(200, json=GITHUB_PAYLOAD)
    # New-ecosystem fan-out yields empty results under this closed-world handler.
    if host == "pkg.go.dev":
        return httpx.Response(200, text="<html></html>")
    if host == "proxy.golang.org":
        return httpx.Response(404, json={})
    if host == "crates.io":
        return httpx.Response(200, json={"crates": []})
    if host == "azuresearch-usnc.nuget.org":
        return httpx.Response(200, json={"data": []})
    raise AssertionError(f"{host}{path}")


def test_npm_exact_enriched_and_ranked_first(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run_coro(svc.search_async("demo", "npm", 10, counting_client(handler, [])))
    assert items[0]["name"] == "demo"
    assert items[0]["exact"] is True
    assert items[0]["docs_url"] == "https://demo.dev/"
    assert items[0]["source"] == "registry"
    assert items[1]["name"] == "demo-helper"
    assert items[1]["docs_url"] is None


def test_repoless_candidate_carries_note(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run_coro(svc.search_async("demo", "npm", 10, counting_client(handler, [])))
    assert "note" not in items[0]
    assert items[1]["repo_url"] is None
    assert "note" in items[1]


def test_maven_exact_enriched_via_metadata(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run_coro(
        svc.search_async("org.demo:demo", "maven", 10, counting_client(handler, []))
    )
    assert items[0]["name"] == "org.demo:demo"
    assert items[0]["exact"] is True
    assert items[0]["docs_url"] == "https://demo.apache.org/"
    assert items[0]["version"] == "2.0"


def test_pypi_exact_only(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run_coro(svc.search_async("Demo", "pypi", 10, counting_client(handler, [])))
    assert len(items) == 1
    assert items[0]["name"] == "demo"
    assert items[0]["docs_url"] == "https://demo.readthedocs.io/"
    unknown = run_coro(
        svc.search_async("no-such-pkg-xyz", "pypi", 10, counting_client(handler, []))
    )
    assert unknown == []


def test_cross_eco_fanout_and_github_gated(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(
        conn,
        make_discovery_settings(monkeypatch, tmp_path, discovery_github_token="tok"),
    )
    items = run_coro(svc.search_async("demo", None, 10, counting_client(handler, [])))
    ecos = {c["ecosystem"] for c in items}
    assert {"npm", "maven", "pypi", "github"} <= ecos
    assert items[0]["docs_url"]


def test_github_skipped_without_token(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run_coro(svc.search_async("demo", None, 10, counting_client(handler, [])))
    assert "github" not in {c["ecosystem"] for c in items}


def test_cache_hit_makes_no_request(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    calls: list = []
    first = run_coro(
        svc.search_async("demo", "npm", 10, counting_client(handler, calls))
    )
    assert calls
    calls.clear()
    second = run_coro(
        svc.search_async("demo", "npm", 10, counting_client(handler, calls))
    )
    assert calls == []
    assert second == first


def test_offline_cache_miss_is_retryable(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(
        conn, make_discovery_settings(monkeypatch, tmp_path, discovery_offline=True)
    )
    with pytest.raises(RegistryError, match="offline cache miss"):
        run_coro(svc.search_async("demo", "npm", 10, counting_client(handler, [])))


def test_disabled_and_invalid_inputs(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(
        conn, make_discovery_settings(monkeypatch, tmp_path, discovery_enabled=False)
    )
    with pytest.raises(ValueError, match="disabled"):
        run_coro(svc.search_async("demo", None, 10, counting_client(handler, [])))
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    with pytest.raises(ValueError, match="empty"):
        run_coro(svc.search_async("  ", None, 10, counting_client(handler, [])))
    with pytest.raises(ValueError, match="unsupported ecosystem"):
        run_coro(svc.search_async("demo", "rubygems", 10, counting_client(handler, [])))
    with pytest.raises(ValueError, match="between 1 and 100"):
        run_coro(svc.search_async("demo", None, 0, counting_client(handler, [])))


def test_ecosystem_aliases_share_canonical_cache(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    calls: list = []
    via_alias = run_coro(
        svc.search_async("demo", "py", 10, counting_client(handler, calls))
    )
    assert calls
    assert via_alias[0]["ecosystem"] == "pypi"
    calls.clear()
    via_canonical = run_coro(
        svc.search_async("demo", "pypi", 10, counting_client(handler, calls))
    )
    assert calls == []
    assert via_canonical == via_alias


def test_js_alias_matches_npm(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run_coro(svc.search_async("demo", "js", 10, counting_client(handler, [])))
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
    items = run_coro(svc.search_async("demo", "pypi", 10, client))
    assert [c["name"] for c in items] == ["demo", "demo-helper"]
    assert items[0]["exact"] is True
    assert items[0]["docs_url"] == "https://demo.readthedocs.io/"
    assert items[1]["source"] == "search:pypi"


def test_npm_search_failure_falls_back_to_exact_snapshot(
    conn, monkeypatch, tmp_path
) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))

    def failing_search(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "registry.npmjs.org" and path == "/-/v1/search":
            return httpx.Response(500, json={})
        if host == "registry.npmjs.org" and path == "/demo":
            return httpx.Response(200, json=NPM_SNAPSHOT)
        raise AssertionError(f"{host}{path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(failing_search))
    try:
        items = run_coro(svc.search_async("demo", "npm", 10, client))
    finally:
        run_coro(client.aclose())
    assert len(items) == 1
    assert items[0]["name"] == "demo"
    assert items[0]["source"] == "registry"
    assert items[0]["version"] == "3.0.0"


def test_npm_search_level_addresses_sanitized(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    payload = {
        "objects": [
            {
                "package": {
                    "name": "demo",
                    "version": "1.0.0",
                    "description": "demo",
                    "links": {
                        "homepage": "http://192.168.1.10/docs",
                        "repository": "http://192.168.1.10/demo",
                    },
                },
                "score": {"final": 1.0},
                "downloads": {"monthly": 3},
            }
        ]
    }

    def private_search(request: httpx.Request) -> httpx.Response:
        if request.url.host == "registry.npmjs.org":
            return httpx.Response(200, json=payload)
        raise AssertionError(f"{request.url.host}{request.url.path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(private_search))
    try:
        items = run_coro(svc.search_async("unrelated-query", "npm", 10, client))
    finally:
        run_coro(client.aclose())
    assert items[0]["homepage"] is None
    assert items[0]["repo_url"] is None
    assert items[0]["docs_url"] is None


def test_go_search_enriches_missing_exact(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))

    def go_handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "pkg.go.dev":
            return httpx.Response(
                200, text='<a href="/github.com/acme/other">other</a>'
            )
        if host == "proxy.golang.org" and path == "/github.com/acme/demo/@latest":
            return httpx.Response(200, json={"Version": "v1.2.3"})
        if host == "proxy.golang.org" and path == "/github.com/acme/demo/@v/list":
            return httpx.Response(200, text="v1.2.3\n")
        raise AssertionError(f"{host}{path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(go_handler))
    try:
        items = run_coro(svc.search_async("github.com/acme/demo", "go", 10, client))
    finally:
        run_coro(client.aclose())
    exact = [i for i in items if i["exact"]]
    assert exact and exact[0]["source"] == "registry"
    assert exact[0]["name"] == "github.com/acme/demo"
    assert exact[0]["repo_url"] == "https://github.com/acme/demo"


# Go, crates and nuget discovery plus six-way fanout.
NEW_ECO_NPM_SEARCH = {
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

NEW_ECO_MAVEN_SEARCH = {
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

NEW_ECO_MAVEN_METADATA = """<?xml version="1.0" encoding="UTF-8"?>\n<metadata>\n  <versioning>\n    <release>1.0</release>\n    <versions><version>1.0</version></versions>\n  </versioning>\n</metadata>\n"""

NEW_ECO_MAVEN_POM = """<?xml version="1.0" encoding="UTF-8"?>\n<project>\n  <url>https://demo.apache.org/</url>\n</project>\n"""

NEW_ECO_PYPI_PAYLOAD = {
    "info": {
        "version": "1.0.0",
        "home_page": "https://demo.readthedocs.io/",
        "project_urls": {"Documentation": "https://demo.readthedocs.io/"},
    },
    "releases": {"1.0.0": []},
}

NEW_ECO_GO_SEARCH_HTML = (
    '<a href="/github.com/acme/demo">demo</a><a href="/github.com/acme/other">other</a>'
)

NEW_ECO_GO_LATEST = {"Version": "v1.2.3"}
NEW_ECO_GO_LIST = "v1.0.0\nv1.2.3\n"

NEW_ECO_CRATES_SEARCH = {
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

NEW_ECO_CRATES_SNAPSHOT = {
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

NEW_ECO_NUGET_QUERY = {
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

NEW_ECO_NUGET_REG = {
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


def new_eco_handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    path = request.url.path
    query = dict(request.url.params)
    if host == "registry.npmjs.org" and path == "/-/v1/search":
        return httpx.Response(200, json=NEW_ECO_NPM_SEARCH)
    if host == "search.maven.org":
        return httpx.Response(200, json=NEW_ECO_MAVEN_SEARCH)
    if host == "repo1.maven.org" and path.endswith("maven-metadata.xml"):
        return httpx.Response(200, text=NEW_ECO_MAVEN_METADATA)
    if host == "repo1.maven.org" and path.endswith(".pom"):
        return httpx.Response(200, text=NEW_ECO_MAVEN_POM)
    if host == "pypi.org" and path == "/pypi/demo/json":
        return httpx.Response(200, json=NEW_ECO_PYPI_PAYLOAD)
    if host == "pypi.org" and path == "/search":
        return httpx.Response(200, text="<html></html>")
    if host == "pypi.org":
        return httpx.Response(404, json={})
    if host == "pkg.go.dev" and path == "/search":
        if query.get("q") == "example.com/only-proxy":
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, text=NEW_ECO_GO_SEARCH_HTML)
    if host == "proxy.golang.org":
        if path == "/github.com/acme/demo/@latest":
            return httpx.Response(200, json=NEW_ECO_GO_LATEST)
        if path == "/github.com/acme/demo/@v/list":
            return httpx.Response(200, text=NEW_ECO_GO_LIST)
        if path == "/example.com/only-proxy/@latest":
            return httpx.Response(200, json={"Version": "v0.1.0"})
        if path == "/example.com/only-proxy/@v/list":
            return httpx.Response(200, text="v0.1.0\n")
        return httpx.Response(404, json={})
    if host == "crates.io" and path == "/api/v1/crates":
        return httpx.Response(200, json=NEW_ECO_CRATES_SEARCH)
    if host == "crates.io" and path == "/api/v1/crates/demo":
        return httpx.Response(200, json=NEW_ECO_CRATES_SNAPSHOT)
    if host == "azuresearch-usnc.nuget.org":
        return httpx.Response(200, json=NEW_ECO_NUGET_QUERY)
    if (
        host == "api.nuget.org"
        and path == "/v3/registration5-gz-semver2/demo/index.json"
    ):
        return httpx.Response(200, json=NEW_ECO_NUGET_REG)
    raise AssertionError(f"{host}{path}")


def test_go_search_enriched_via_proxy(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run_coro(
        svc.search_async(
            "github.com/acme/demo", "go", 10, counting_client(new_eco_handler, [])
        )
    )
    assert items[0]["name"] == "github.com/acme/demo"
    assert items[0]["exact"] is True
    assert items[0]["version"] == "v1.2.3"
    assert items[0]["docs_url"] == "https://pkg.go.dev/github.com/acme/demo@v1.2.3"
    assert items[0]["source"] == "registry"
    assert items[1]["name"] == "github.com/acme/other"


def test_go_exact_only_fallback(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run_coro(
        svc.search_async(
            "example.com/only-proxy", "go", 10, counting_client(new_eco_handler, [])
        )
    )
    assert len(items) == 1
    assert items[0]["exact"] is True
    assert items[0]["version"] == "v0.1.0"


def test_crates_search_enriched(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run_coro(
        svc.search_async("demo", "crates", 10, counting_client(new_eco_handler, []))
    )
    assert items[0]["name"] == "demo"
    assert items[0]["exact"] is True
    assert items[0]["docs_url"] == "https://docs.rs/demo/2.0.0"
    assert items[0]["source"] == "registry"


def test_nuget_search_enriched(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run_coro(
        svc.search_async("Demo", "nuget", 10, counting_client(new_eco_handler, []))
    )
    assert items[0]["name"] == "Demo"
    assert items[0]["exact"] is True
    assert items[0]["docs_url"] == "https://demo.dev/"
    assert items[0]["source"] == "registry"


def test_cross_eco_fanout_covers_six(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    items = run_coro(
        svc.search_async("demo", None, 20, counting_client(new_eco_handler, []))
    )
    assert {"npm", "maven", "pypi", "go", "crates", "nuget"} <= {
        c["ecosystem"] for c in items
    }


def test_new_eco_aliases_share_cache(conn, monkeypatch, tmp_path) -> None:
    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    calls: list = []
    via_alias = run_coro(
        svc.search_async(
            "github.com/acme/demo",
            "golang",
            10,
            counting_client(new_eco_handler, calls),
        )
    )
    assert calls
    assert via_alias[0]["ecosystem"] == "go"
    calls.clear()
    via_canonical = run_coro(
        svc.search_async(
            "github.com/acme/demo", "go", 10, counting_client(new_eco_handler, calls)
        )
    )
    assert calls == []
    assert via_canonical == via_alias

    calls.clear()
    via_cargo = run_coro(
        svc.search_async("demo", "cargo", 10, counting_client(new_eco_handler, calls))
    )
    assert calls
    assert via_cargo[0]["ecosystem"] == "crates"
    calls.clear()
    via_crates = run_coro(
        svc.search_async("demo", "crates", 10, counting_client(new_eco_handler, calls))
    )
    assert calls == []
    assert via_crates == via_cargo


def test_new_eco_search_failure_is_retryable(conn, monkeypatch, tmp_path) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        if request.url.host == "crates.io":
            return httpx.Response(500, json={})
        return new_eco_handler(request)

    svc = DiscoveryService(conn, make_discovery_settings(monkeypatch, tmp_path))
    with pytest.raises(RegistryError) as excinfo:
        run_coro(
            svc.search_async(
                "demo",
                "crates",
                10,
                httpx.AsyncClient(transport=httpx.MockTransport(failing)),
            )
        )
    assert excinfo.value.kind == "retryable"
