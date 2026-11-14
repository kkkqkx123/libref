"""Issue fixes: scope-aware discovery, nav structure links, loud budgets, site versions.

Pure in-memory coverage for the fetch-chain repairs; no network, no database.
"""

from __future__ import annotations

import types

import httpx
import pytest

from libref.crawler import UrlSeeder
from libref.crawler.extract import scrape_html
from libref.crawler.scope import BudgetExceeded
from libref.crawler.versioning import compare_versions, majority_evidence
from libref.services.docs.scrape_plan import plan_targets

pytestmark = pytest.mark.unit


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_prefix_hint_picks_in_scope_level_and_reports_it():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/docs/validation/latest/llms.txt":
            links = "\n".join(
                f"- [P{i}](/docs/validation/latest/p{i}.md)" for i in range(9)
            )
            return httpx.Response(200, text=links + "\n")
        if request.url.path == "/docs/validation/latest/guide/llms.txt":
            return httpx.Response(
                200, text="- [One](/docs/validation/latest/guide/one.md)\n"
            )
        return httpx.Response(404)

    seeder = UrlSeeder(_client(handler), robots=None)  # type: ignore[arg-type]
    result = _run(
        seeder.discover(
            "https://docs.example.com/docs/validation/latest/guide/",
            path_prefixes=("/docs/validation/latest/",),
        )
    )
    assert result.source == "llms.txt"
    assert len(result.urls) == 9
    assert result.chosen_llms.endswith("/docs/validation/latest/llms.txt")
    assert result.llms_levels
    assert any(
        name.endswith("/docs/validation/latest/llms.txt") and total == 9 and scoped == 9
        for name, total, scoped in result.llms_levels
    )


def test_nav_links_are_structure_seeds_not_body_links():
    html = (
        "<html><head><title>Guide</title></head><body>"
        "<nav><ul>"
        '<li><a href="/installation.html">Install</a></li>'
        '<li><a href="/collections.html">Collections</a></li>'
        "</ul></nav>"
        "<main><h1>Guide</h1><p>Body text with enough words to survive pruning easily.</p>"
        '<p>See <a href="/next.html">next</a>.</p></main>'
        "</body></html>"
    )
    scraped = scrape_html(html, "https://d.example/guide/")
    assert "https://d.example/next.html" in scraped.internal_links
    assert "https://d.example/installation.html" not in scraped.internal_links
    assert "https://d.example/installation.html" in scraped.structure_links
    assert "Install" not in scraped.markdown


def test_discovery_budget_is_loud():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/llms.txt":
            return httpx.Response(200, text="- [A](/a.html)\n- [B](/b.html)\n")
        if request.url.path == "/llms-full.txt":
            return httpx.Response(404)
        raise AssertionError(request.url.path)

    seeder = UrlSeeder(_client(handler), robots=None)  # type: ignore[arg-type]
    with pytest.raises(BudgetExceeded):
        _run(seeder.discover("https://d.example/", max_pages=1))


def test_plan_targets_budget_is_loud():
    seed = types.SimpleNamespace(
        urls=["https://d.example/a", "https://d.example/b"],
        cross_site=[],
        truncated=False,
    )
    with pytest.raises(BudgetExceeded):
        plan_targets(seed, "https://d.example/", None, 1)


def test_nested_sitemap_over_depth_is_loud():
    doc = (
        "<sitemapindex>"
        "<sitemap><loc>https://d.example/sub.xml</loc></sitemap>"
        "</sitemapindex>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/sitemap.xml", "/sub.xml"):
            return httpx.Response(200, text=doc)
        if request.url.path == "/llms.txt":
            return httpx.Response(404)
        if request.url.path == "/llms-full.txt":
            return httpx.Response(404)
        if "sitemap" in request.url.path:
            return httpx.Response(404)
        raise AssertionError(request.url.path)

    seeder = UrlSeeder(_client(handler), robots=None)  # type: ignore[arg-type]
    with pytest.raises(BudgetExceeded):
        _run(seeder._collect_sitemap("https://d.example/", depth=5))


def test_site_version_majority_and_compare():
    urls = [
        "https://d.example/en/latest/usage/",
        "https://d.example/en/latest/api/",
    ]
    evidence = majority_evidence(urls)
    assert evidence["value"] == "latest"
    assert compare_versions("latest", "latest") == "ok"
    assert compare_versions("v3.x", "latest") == "warn"
    assert compare_versions("v2.x", "v3.x") == "fail"
    assert compare_versions("1.0", "unversioned") == "warn"
