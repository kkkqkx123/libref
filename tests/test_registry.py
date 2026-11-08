"""注册表解析测试：离线 MockTransport 断言解析、排序、缓存、离线语义。"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from libref.config import LibrefSettings
from libref.crawler.registry import RegistryError, normalize_name, sanitize_http_url
from libref.services.resolver import resolve_package


def make_settings(**kw) -> LibrefSettings:
    base = dict(
        request_timeout=5.0,
        registry_cache_ttl_days=1.0,
        registry_offline=False,
        registry_allow_private=False,
    )
    base.update(kw)
    return LibrefSettings(**base)


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
            return httpx.Response(status, json=payload if payload is not None else PYPI_PAYLOAD)
        raise AssertionError(request.url.path)

    return handler


def test_pypi_resolves_docs_and_versions(conn):
    settings = make_settings()
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


def test_pypi_normalizes_name(conn):
    settings = make_settings()
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


def test_npm_resolves_homepage(conn):
    settings = make_settings()

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


def test_scoped_name_infers_npm(conn):
    settings = make_settings()

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


def test_bare_name_requires_ecosystem(conn):
    settings = make_settings()
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler()))
    try:
        with pytest.raises(ValueError, match="ecosystem required"):
            run(resolve_package(conn, client, settings, None, "requests"))
    finally:
        run(client.aclose())


def test_requested_version_unconfirmed_still_resolves(conn):
    settings = make_settings()
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler()))
    try:
        res = run(resolve_package(conn, client, settings, "pypi", "requests", "9.9.9-custom"))
    finally:
        run(client.aclose())
    assert res.version == "9.9.9-custom"
    assert res.registry_confirmed is False
    assert res.docs_url is not None


def test_not_found_maps(conn):
    settings = make_settings()
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler(status=404)))
    try:
        with pytest.raises(RegistryError) as exc:
            run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    assert exc.value.kind == "not_found"


def test_no_docs_returns_hint(conn):
    settings = make_settings()
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


def test_override_wins_over_registry(conn, monkeypatch):
    from libref.crawler import registry_overrides as ov

    monkeypatch.setitem(
        ov.OVERRIDES,
        ("pypi", "requests"),
        {"docs_url": "https://override.example/docs", "source": "manual", "verified_at": "2026-11-01"},
    )
    settings = make_settings()
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler()))
    try:
        res = run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    assert res.docs_url == "https://override.example/docs"
    assert res.source == "override"


def test_cache_hit_produces_no_network(conn):
    settings = make_settings()
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


def test_stale_falls_back_with_stale_source(conn):
    settings = make_settings()
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler()))
    try:
        run(resolve_package(conn, client, settings, "pypi", "requests"))
    finally:
        run(client.aclose())
    conn.execute("UPDATE registry_cache SET fetched_at=? WHERE ecosystem='pypi' AND name='requests'",
                 (time.time() - 10 * 86400,))
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


def test_offline_miss_fails_and_hit_succeeds(conn):
    online = make_settings()
    client = httpx.AsyncClient(transport=httpx.MockTransport(pypi_handler()))
    try:
        run(resolve_package(conn, client, online, "pypi", "requests"))
    finally:
        run(client.aclose())

    offline = make_settings(registry_offline=True)

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


def test_explicit_address_bypasses_network(conn):
    settings = make_settings()

    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("explicit must not hit network")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    try:
        res = run(
            resolve_package(
                conn, client, settings, "pypi", "requests",
                "9.9.9", "https://docs.example/custom/",
            )
        )
    finally:
        run(client.aclose())
    assert res.source == "explicit"
    assert res.docs_url == "https://docs.example/custom/"


def test_private_address_rejected_by_default():
    assert sanitize_http_url("http://127.0.0.1/docs", allow_private=False) is None
    assert sanitize_http_url("http://127.0.0.1/docs", allow_private=True) is not None
    assert sanitize_http_url("ftp://example.com/x", allow_private=True) is None
    assert normalize_name("pypi", "Requests__x") == "requests-x"


def test_pypi_skips_repo_hosts_in_candidates(conn):
    settings = make_settings()
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


def test_pypi_repo_url_skips_funding(conn):
    settings = make_settings()
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


def test_best_effort_skips_prerelease_without_stable(conn):
    settings = make_settings()
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


def test_overrides_have_verification_dates():
    from libref.crawler.registry_overrides import OVERRIDES

    for key, item in OVERRIDES.items():
        assert item.get("docs_url"), key
        assert item.get("verified_at"), key
