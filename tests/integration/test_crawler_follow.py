"""Follow-links integration: breadth-first link following within scope budget."""

from __future__ import annotations

import httpx
import pytest

from libref.crawler.url_seeder import _SEED_CACHE
from libref.services.ingest import IngestService
from libref.services.search import SearchService
from tests.conftest import make_crawler_settings, mock_ingest

pytestmark = pytest.mark.integration


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    host = request.url.host
    if path.endswith(("llms.txt", "llms-full.txt")):
        return httpx.Response(404)
    if path == "/robots.txt":
        return httpx.Response(200, text="")
    if path == "/sitemap.xml":
        return httpx.Response(
            200,
            text=(
                '<?xml version="1.0"?><urlset>'
                f"<url><loc>https://{host}/index.html</loc></url></urlset>"
            ),
        )
    if path == "/index.html":
        return httpx.Response(
            200,
            text=(
                "<html><body><main><h1>Index</h1>"
                "<p>Index body with enough words to survive pruning easily.</p>"
                f'<p>Next <a href="https://{host}/deep.html">deep page</a></p>'
                "</main></body></html>"
            ),
        )
    if path == "/deep.html":
        return httpx.Response(
            200,
            text=(
                "<html><body><main><h1>Deep</h1>"
                "<p>Deep body with enough words to survive pruning easily here.</p>"
                "</main></body></html>"
            ),
        )
    if "sitemap" in path:
        return httpx.Response(404)
    raise AssertionError(path)


def test_follow_off_by_default(conn, monkeypatch, tmp_path) -> None:
    _SEED_CACHE.clear()
    settings = make_crawler_settings(monkeypatch, tmp_path)
    with mock_ingest(_handler):
        counts = IngestService(settings, conn).scrape_docs(
            "docs", "1.0", "https://d.example/"
        )
    assert counts["stored"] == 1
    assert counts["deep_discovered"] == 0
    urls = [hit["url"] for hit in SearchService(conn).search_docs("Deep body")]
    assert "https://d.example/deep.html" not in urls


def test_follow_discovers_deep_page(conn, monkeypatch, tmp_path) -> None:
    _SEED_CACHE.clear()
    settings = make_crawler_settings(monkeypatch, tmp_path)
    with mock_ingest(_handler):
        counts = IngestService(settings, conn).scrape_docs(
            "docs",
            "1.0",
            "https://d.example/",
            scope={"follow_links": True},
        )
    assert counts["stored"] == 2
    assert counts["deep_discovered"] == 1
    hits = SearchService(conn).search_docs("Deep body")
    assert hits and hits[0]["url"].endswith("/deep.html")


def test_follow_respects_max_pages(conn, monkeypatch, tmp_path) -> None:
    _SEED_CACHE.clear()
    settings = make_crawler_settings(monkeypatch, tmp_path)
    with mock_ingest(_handler):
        counts = IngestService(settings, conn).scrape_docs(
            "docs",
            "1.0",
            "https://d.example/",
            max_pages=1,
            scope={"follow_links": True},
        )
    assert counts["stored"] == 1
    assert counts["deep_truncated"] == 1
