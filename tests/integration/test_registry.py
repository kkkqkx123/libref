"""Registry parsing tests: offline MockTransport assertions for parsing, ordering, caching, offline semantics."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date

import httpx
import pytest

from libref.crawler.registry import RegistryError, normalize_name, sanitize_http_url
from libref.crawler.registry import overrides as _overrides_state
from libref.services.registry.resolver import resolve_package
from tests.conftest import make_registry_settings

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clean_file_overrides():
    _overrides_state.clear_file_overrides()
    yield
    _overrides_state.clear_file_overrides()


def run(coro):
    return asyncio.run(coro)


PYPI_PAYLOAD = {
    "info": {
        "version": "2.31.0",
        "home_page": "https://requests.readthedocs.io/",
        "project_urls": {
            "Documentation": "https://requests.readthedocs.io/",
            "Homepage": "https://example.com/other",
            "Source": "https://github.com/psf/requests",
        },
    },
    "releases": {"2.30.0": [], "2.31.0": []},
}

NPM_PAYLOAD = {
    "dist-tags": {"latest": "5.0.0"},
    "versions": {"4.0.0": {}, "5.0.0": {}},
    "homepage": "https://expressjs.com/",
    "repository": {"url": "git+https://github.com/expressjs/express.git"},
}


def pypi_handler(payload=None, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/pypi/requests/json":
            return httpx.Response(
                status, json=payload if payload is not None else PYPI_PAYLOAD
            )
        raise AssertionError(request.url.path)

    return handler


def test_pypi_resolves_docs_and_versions(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler()))
    try:
        res = run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    assert res.docs_url == "https://requests.readthedocs.io/"
    assert res.version == "2.31.0"
    assert res.registry_confirmed is True
    assert res.source == "registry"
    assert "2.30.0" in [v for v in res.candidates] or True


def test_pypi_normalizes_name(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=PYPI_PAYLOAD)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        res = run(resolve_package(conn, client, settings, "pypi", "Requests"))
    finally:
        run(client.aclose())
    assert res.name == "requests"
    assert seen == ["/pypi/requests/json"]


def test_npm_resolves_homepage(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "registry.npmjs.org"
        return httpx.Response(200, json=NPM_PAYLOAD)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        res = run(resolve_package(conn, client, settings, "npm", "express"))
    finally:
        run(client.aclose())
    assert res.docs_url == "https://expressjs.com/"
    assert res.version == "5.0.0"


def test_scoped_name_infers_npm(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "dist-tags": {"latest": "1.0.0"},
                "versions": {"1.0.0": {}},
                "homepage": "https://example.com/docs",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        res = run(resolve_package(conn, client, settings, None, "@scope/pkg"))
    finally:
        run(client.aclose())
    assert res.ecosystem == "npm"


def test_bare_name_requires_ecosystem(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler()))
    try:
        with pytest.raises(ValueError, match="ecosystem required"):
            run(resolve_package(conn, client, settings, None, "requests"))
    finally:
        run(client.aclose())


def test_requested_version_unconfirmed_still_resolves(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler()))
    try:
        res = run(
            resolve_package(conn, client, settings, "pypi", "requests", "9.9.9-custom")
        )
    finally:
        run(client.aclose())
    assert res.version == "9.9.9-custom"
    assert res.registry_confirmed is False
    assert res.docs_url is not None


def test_not_found_maps(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler(status=404)))
    try:
        with pytest.raises(RegistryError) as exc:
            run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    assert exc.value.kind == "not_found"


def test_no_docs_returns_hint(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)
    payload = {
        "info": {
            "version": "1.0",
            "home_page": "https://example.com/marketing",
            "project_urls": {"Source": "https://github.com/org/repo"},
        },
        "releases": {"1.0": []},
    }
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler(payload)))
    try:
        with pytest.raises(RegistryError) as exc:
            run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    assert exc.value.kind == "no_docs"
    assert "repo_url" in exc.value.hint


def test_no_docs_hint_carries_context(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)
    payload = {
        "info": {
            "version": "1.0",
            "home_page": "https://example.com/marketing",
            "project_urls": {"Source": "https://github.com/org/repo"},
        },
        "releases": {"1.0": []},
    }
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler(payload)))
    try:
        with pytest.raises(RegistryError) as exc:
            run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    assert exc.value.hint.get("ecosystem") == "pypi"
    assert exc.value.hint.get("name") == "requests"
    assert exc.value.hint.get("repo_url") == "https://github.com/org/repo"


def test_not_found_hint_carries_context(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler(status=404)))
    try:
        with pytest.raises(RegistryError) as exc:
            run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    assert exc.value.kind == "not_found"
    assert exc.value.hint.get("ecosystem") == "pypi"
    assert exc.value.hint.get("name") == "requests"


def test_override_wins_over_registry(conn, monkeypatch, tmp_path):
    from libref.crawler.registry import overrides as ov

    monkeypatch.setitem(
        ov.OVERRIDES,
        ("pypi", "requests"),
        {
            "docs_url": "https://override.example/docs",
            "source": "manual",
            "verified_at": "2026-11-01",
        },
    )
    settings = make_registry_settings(monkeypatch, tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler()))
    try:
        res = run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    assert res.docs_url == "https://override.example/docs"
    assert res.source == "override"


def test_cache_hit_produces_no_network(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler()))
    try:
        first = run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    assert first.source == "registry"

    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not hit network on fresh cache")

    client2 = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    try:
        second = run(resolve_package(conn, client2, settings, "pypi", "requests"))
    finally:
        run(client2.aclose())
    assert second.docs_url == first.docs_url


def test_stale_falls_back_with_stale_source(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler()))
    try:
        run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    conn.execute(
        "UPDATE registry_cache SET fetched_at=? WHERE ecosystem='pypi' AND name='requests'",
        (time.time() - 10 * 86400,),
    )
    conn.commit()

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    client2 = httpx.AsyncClient(transport=httpx.MockTransport(failing))
    try:
        res = run(resolve_package(conn, client2, settings, "pypi", "requests"))
    finally:
        run(client2.aclose())
    assert res.source == "registry-stale"
    assert res.docs_url == "https://requests.readthedocs.io/"


def test_offline_miss_fails_and_hit_succeeds(conn, monkeypatch, tmp_path):
    online = make_registry_settings(monkeypatch, tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler()))
    try:
        run(resolve_package(conn, client, online, "pypi", "requests"))
    finally:
        run(client.aclose())

    offline = make_registry_settings(monkeypatch, tmp_path, registry_offline=True)

    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("offline must not hit network")

    client2 = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    try:
        res = run(resolve_package(conn, client2, offline, "pypi", "requests"))
        assert res.docs_url is not None
        with pytest.raises(RegistryError):
            run(resolve_package(conn, client2, offline, "pypi", "nosuchcached"))
    finally:
        run(client2.aclose())


def test_explicit_address_bypasses_network(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)

    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("explicit must not hit network")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    try:
        res = run(
            resolve_package(
                conn,
                client,
                settings,
                "pypi",
                "requests",
                "9.9.9",
                "https://docs.example/custom/",
            )
        )
    finally:
        run(client.aclose())
    assert res.source == "explicit"
    assert res.docs_url == "https://docs.example/custom/"


def test_private_address_rejected_by_default(monkeypatch, tmp_path):
    assert sanitize_http_url("http://127.0.0.1/docs", allow_private=False) is None
    assert sanitize_http_url("http://127.0.0.1/docs", allow_private=True) is not None
    assert sanitize_http_url("ftp://example.com/x", allow_private=True) is None
    assert normalize_name("pypi", "Requests__x") == "requests-x"


def test_pypi_skips_repo_hosts_in_candidates(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)
    payload = {
        "info": {
            "version": "0.28.1",
            "home_page": "https://github.com/encode/httpx",
            "project_urls": {
                "Changelog": "https://github.com/encode/httpx/blob/master/CHANGELOG.md",
                "Documentation": "https://www.python-httpx.org",
                "Homepage": "https://github.com/encode/httpx",
                "Source": "https://github.com/encode/httpx",
            },
        },
        "releases": {"0.28.1": []},
    }
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler(payload)))
    try:
        res = run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    assert res.docs_url == "https://www.python-httpx.org"
    assert all("github.com" not in c for c in res.candidates)
    assert res.repo_url == "https://github.com/encode/httpx"


def test_pypi_repo_url_skips_funding(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)
    payload = {
        "info": {
            "version": "2.14.3",
            "home_page": "https://github.com/pydantic/pydantic",
            "project_urls": {
                "Documentation": "https://pydantic.dev/docs/validation/latest/get-started/",
                "Funding": "https://github.com/sponsors/samuelcolvin",
                "Source": "https://github.com/pydantic/pydantic",
            },
        },
        "releases": {"2.14.3": []},
    }
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler(payload)))
    try:
        res = run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    assert res.repo_url == "https://github.com/pydantic/pydantic"


def test_best_effort_skips_prerelease_without_stable(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)
    payload = {
        "dist-tags": {},
        "versions": {"1.0.0": {}, "2.0.0-canary-1": {}, "1.5.0": {}},
        "homepage": "https://example.com/docs",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        res = run(resolve_package(conn, client, settings, "npm", "pkg"))
    finally:
        run(client.aclose())
    assert res.version == "1.5.0"
    assert res.registry_confirmed is True


def test_overrides_have_verification_dates(monkeypatch, tmp_path):
    from libref.crawler.registry.overrides import all_overrides

    for key, item in all_overrides().items():
        assert item.get("docs_url") or item.get("repo_url"), key
        assert item.get("verified_at"), key


def test_registry_bases_mirror_routes_to_custom_host(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(
        monkeypatch,
        tmp_path,
        registry_bases={
            "pypi": "https://mirror.example/pypi",
            "npm": "https://registry.npmjs.org",
        },
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.host}{request.url.path}")
        return httpx.Response(200, json=PYPI_PAYLOAD)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        res = run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    assert seen == ["mirror.example/pypi/requests/json"]
    assert res.docs_url == "https://requests.readthedocs.io/"


def test_registry_mirror_cn_routes_to_preset_hosts(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path, registry_mirror="cn")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.host}{request.url.path}")
        return httpx.Response(200, json=NPM_PAYLOAD)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        res = run(resolve_package(conn, client, settings, "npm", "express"))
    finally:
        run(client.aclose())
    assert seen == ["registry.npmmirror.com/express"]
    assert res.docs_url == "https://expressjs.com/"


def test_registry_bases_explicit_wins_over_mirror(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(
        monkeypatch,
        tmp_path,
        registry_mirror="cn",
        registry_bases={"npm": "https://custom.example/npm"},
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.host}{request.url.path}")
        return httpx.Response(200, json=NPM_PAYLOAD)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        run(resolve_package(conn, client, settings, "npm", "express"))
    finally:
        run(client.aclose())
    assert seen == ["custom.example/npm/express"]


def test_unknown_registry_mirror_rejected(monkeypatch, tmp_path):
    from libref.crawler.registry import registry_base_for

    settings = make_registry_settings(monkeypatch, tmp_path, registry_mirror="nope")
    with pytest.raises(ValueError, match="unknown registry mirror"):
        registry_base_for("npm", settings)


def test_file_override_wins_over_builtin(conn, tmp_path, monkeypatch):
    from libref.crawler.registry import overrides as ov

    monkeypatch.setitem(
        ov.OVERRIDES,
        ("pypi", "requests"),
        {
            "docs_url": "https://builtin.example/docs",
            "source": "manual",
            "verified_at": "2026-11-01",
        },
    )
    override_file = tmp_path / "overrides.json"
    override_file.write_text(
        json.dumps(
            [
                {
                    "ecosystem": "pypi",
                    "name": "requests",
                    "docs_url": "https://file.example/docs",
                    "source": "file",
                    "verified_at": "2026-11-02",
                }
            ]
        ),
        encoding="utf-8",
    )
    settings = make_registry_settings(
        monkeypatch, tmp_path, registry_overrides_file=override_file
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler()))
    try:
        res = run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    assert res.docs_url == "https://file.example/docs"
    assert res.source == "override"


def test_overrides_file_invalid_rejected(tmp_path, monkeypatch):
    from libref.crawler.registry.overrides import load_overrides_file

    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            [
                {
                    "ecosystem": "pypi",
                    "name": "requests",
                    "docs_url": "ftp://example.com/x",
                    "verified_at": "2026-11-01",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_overrides_file(bad)


def test_stale_overrides_gate_lists_expired(monkeypatch, tmp_path):
    from libref.crawler.registry.overrides import (
        find_stale_overrides,
        is_override_stale,
        set_file_overrides,
    )

    assert is_override_stale("2020-01-01", 180.0, date(2026, 11, 8)) is True
    assert is_override_stale("2026-11-01", 180.0, date(2026, 11, 8)) is False
    assert is_override_stale("not-a-date", 180.0, date(2026, 11, 8)) is True
    set_file_overrides(
        {
            ("pypi", "old"): {
                "docs_url": "https://example.com/old",
                "source": "file",
                "verified_at": "2020-01-01",
            },
            ("pypi", "fresh"): {
                "docs_url": "https://example.com/fresh",
                "source": "file",
                "verified_at": "2026-11-01",
            },
        }
    )
    stale = find_stale_overrides(180.0, date(2026, 11, 8))
    assert [k for k, _ in stale] == [("pypi", "old")]


MAVEN_METADATA = (
    "<metadata><groupId>org.springframework</groupId>"
    "<artifactId>spring-core</artifactId><versioning><release>6.1.0</release>"
    "<versions><version>6.0.0</version><version>6.1.0</version></versions>"
    "</versioning></metadata>"
)
MAVEN_POM = (
    "<project><url>https://spring.io/projects/spring-framework</url>"
    "<scm><url>scm:git:https://github.com/spring-projects/spring-framework.git"
    "</url></scm></project>"
)


def maven_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("maven-metadata.xml"):
        return httpx.Response(200, text=MAVEN_METADATA)
    if request.url.path.endswith(".pom"):
        return httpx.Response(200, text=MAVEN_POM)
    raise AssertionError(request.url.path)


def test_maven_resolves_docs_and_versions(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(maven_handler))
    try:
        res = run(
            resolve_package(
                conn, client, settings, "maven", "org.springframework:spring-core"
            )
        )
    finally:
        run(client.aclose())
    assert res.ecosystem == "maven"
    assert res.version == "6.1.0"
    assert res.docs_url == "https://spring.io/projects/spring-framework"
    assert res.repo_url == "https://github.com/spring-projects/spring-framework.git"
    assert res.source == "registry"


def test_maven_infers_from_colon_and_rejects_bare(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(maven_handler))
    try:
        res = run(
            resolve_package(
                conn, client, settings, None, "org.springframework:spring-core"
            )
        )
    finally:
        run(client.aclose())
    assert res.ecosystem == "maven"
    with pytest.raises(ValueError, match="maven name must look like"):
        run(resolve_package(conn, client, settings, "maven", "spring-core"))


def test_npm_repo_homepage_excluded_from_candidates(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "dist-tags": {"latest": "1.0.0"},
                "versions": {"1.0.0": {}},
                "homepage": "https://github.com/org/repo",
                "repository": {"url": "git+https://github.com/org/repo.git"},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RegistryError) as exc:
            run(resolve_package(conn, client, settings, "npm", "pkg"))
    finally:
        run(client.aclose())
    assert exc.value.kind == "no_docs"
    assert exc.value.hint.get("repo_url") == "https://github.com/org/repo.git"


def test_repo_only_override_does_not_claim_docs(conn, monkeypatch, tmp_path):
    from libref.crawler.registry import overrides as ov

    monkeypatch.setitem(
        ov.OVERRIDES,
        ("pypi", "requests"),
        {
            "repo_url": "https://github.com/org/other",
            "source": "manual",
            "verified_at": "2026-11-01",
        },
    )
    settings = make_registry_settings(monkeypatch, tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler()))
    try:
        res = run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    assert res.source == "registry"
    assert res.docs_url == "https://requests.readthedocs.io/"
    assert res.repo_url == "https://github.com/org/other"


def test_stale_cache_with_override_reports_override_stale(conn, monkeypatch, tmp_path):
    from libref.crawler.registry import overrides as ov

    settings = make_registry_settings(monkeypatch, tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler()))
    try:
        run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    conn.execute(
        "UPDATE registry_cache SET fetched_at=? WHERE ecosystem='pypi' AND name='requests'",
        (time.time() - 10 * 86400,),
    )
    conn.commit()
    monkeypatch.setitem(
        ov.OVERRIDES,
        ("pypi", "requests"),
        {
            "docs_url": "https://override.example/docs",
            "source": "manual",
            "verified_at": "2026-11-01",
        },
    )

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    client2 = httpx.AsyncClient(transport=httpx.MockTransport(failing))
    try:
        res = run(resolve_package(conn, client2, settings, "pypi", "requests"))
    finally:
        run(client2.aclose())
    assert res.source == "override-stale"
    assert res.docs_url == "https://override.example/docs"


def test_repo_missing_reports_no_repo_with_docs_hint(conn, monkeypatch, tmp_path):
    from libref.services.registry.resolver import resolve_repo

    settings = make_registry_settings(monkeypatch, tmp_path)
    payload = {
        "info": {
            "version": "1.0",
            "home_page": "https://example.com/",
            "project_urls": {"Documentation": "https://docs.example/"},
        },
        "releases": {"1.0": []},
    }
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler(payload)))
    try:
        with pytest.raises(RegistryError) as exc:
            run(resolve_repo(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    assert exc.value.kind == "no_repo"
    assert exc.value.hint.get("docs_url") == "https://docs.example/"
    assert exc.value.hint.get("candidates") == ["https://docs.example/"]


def test_missing_overrides_file_warns_and_continues(conn, monkeypatch, tmp_path):
    settings = make_registry_settings(
        monkeypatch, tmp_path, registry_overrides_file=tmp_path / "gone.json"
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler()))
    try:
        res = run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    assert res.source == "registry"
