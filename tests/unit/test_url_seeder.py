"""UrlSeeder tests: local fixtures (httpx MockTransport, no network).

Golden samples: llms.txt format follows the llmstxt.org spec; sitemap structure follows the sitemap.org protocol.
"""

from __future__ import annotations

import gzip
import sqlite3

import httpx

from libref.crawler import RobotsChecker, UrlSeeder
from libref.crawler.url_seeder import _SEED_CACHE


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    import asyncio

    return asyncio.run(coro)


def test_llms_fullest_level_wins_over_section_file(monkeypatch, tmp_path):
    """A section llms.txt must not shadow the fuller product listing above it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/docs/latest/llms.txt":
            links = "\n".join(
                f"- [Page {i}](/docs/latest/p{i}.md)" for i in range(5)
            )
            return httpx.Response(200, text=links + "\n")
        if request.url.path == "/docs/latest/guide/llms.txt":
            return httpx.Response(
                200, text="- [One](/docs/latest/guide/one.md)\n"
            )
        return httpx.Response(404)

    client = make_client(handler)
    seeder = UrlSeeder(client, robots=None)  # type: ignore[arg-type]
    result = run(
        seeder._from_llms_txt("https://docs.example.com/docs/latest/guide/")
    )
    assert result.source == "llms.txt"
    assert result.urls == [
        f"https://docs.example.com/docs/latest/p{i}.md" for i in range(5)
    ]


def test_llms_txt_preferred_over_sitemap(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/llms.txt":
            return httpx.Response(
                200, text="- [Install](/install.html): how to install\n"
            )
        if request.url.path == "/llms-full.txt":
            return httpx.Response(404)
        raise AssertionError("sitemap should not be fetched when llms.txt exists")

    client = make_client(handler)
    seeder = UrlSeeder(client, robots=None)  # type: ignore[arg-type]
    result = run(seeder._from_llms_txt("https://docs.example.com"))
    assert result.source == "llms.txt"
    assert result.urls == ["https://docs.example.com/install.html"]


def test_sitemap_gz_and_index(monkeypatch, tmp_path):
    child_sitemap = gzip.compress(
        b"<?xml version='1.0'?><urlset><url><loc>https://d.example/a.html</loc></url>"
        b"<url><loc>https://d.example/b.html</loc></url></urlset>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(404)
        if request.url.path == "/sitemap.xml.gz":
            return httpx.Response(404)
        if request.url.path == "/sitemap_index.xml":
            return httpx.Response(
                200,
                text=(
                    "<sitemapindex><sitemap><loc>https://d.example/sub.xml.gz</loc></sitemap></sitemapindex>"
                ),
            )
        if request.url.path == "/sub.xml.gz":
            return httpx.Response(200, content=child_sitemap)
        raise AssertionError(request.url.path)

    client = make_client(handler)
    seeder = UrlSeeder(client, robots=None)  # type: ignore[arg-type]
    result = run(seeder._from_sitemap("https://d.example"))
    assert result.source == "sitemap"
    assert result.urls == ["https://d.example/a.html", "https://d.example/b.html"]


def test_discover_filters_and_dedup(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/llms.txt":
            return httpx.Response(
                200,
                text=(
                    "- [A](https://docs.example.com/a.html)\n"
                    "- [Dup](https://docs.example.com/a.html)\n"
                    "- [Secret](https://docs.example.com/private.html)\n"
                    "- [Offsite](https://other.example.com/x.html)\n"
                ),
            )
        if p == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        if p == "/llms-full.txt":
            return httpx.Response(404)
        raise AssertionError(request.url.path)

    _SEED_CACHE.clear()
    client = make_client(handler)
    conn = sqlite3.connect(":memory:")
    robots = RobotsChecker(conn, client)
    seeder = UrlSeeder(client, robots)
    result = run(seeder.discover("https://docs.example.com/"))
    assert result.urls == ["https://docs.example.com/a.html"]
    conn.close()


def test_discover_head_prefilter(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/llms.txt":
            return httpx.Response(
                200,
                text=(
                    "- [A](https://docs.example.com/a.html)\n"
                    "- [B](https://docs.example.com/missing.html)\n"
                ),
            )
        if p == "/robots.txt":
            return httpx.Response(200, text="")
        if p == "/llms-full.txt":
            return httpx.Response(404)
        if p == "/a.html" and request.method == "HEAD":
            return httpx.Response(200, headers={"content-type": "text/html"})
        if p == "/missing.html" and request.method == "HEAD":
            return httpx.Response(404)
        raise AssertionError(request.url.path)

    _SEED_CACHE.clear()
    client = make_client(handler)
    conn = sqlite3.connect(":memory:")
    robots = RobotsChecker(conn, client)
    seeder = UrlSeeder(client, robots)
    result = run(seeder.discover("https://docs.example.com/", prefilter=True))
    assert result.urls == ["https://docs.example.com/a.html"]
    conn.close()


def test_llms_anchor_variants_collapse_to_one_page(tmp_path):
    """An llms.txt naming one page per heading must not spend the page budget per heading."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/llms.txt":
            return httpx.Response(
                200,
                text=(
                    "- [API](https://docs.example.com/api)\n"
                    "- [Strings](https://docs.example.com/api?id=strings)\n"
                    "- [Enums](https://docs.example.com/api?id=enums)\n"
                    "- [Basics](https://docs.example.com/basics)\n"
                ),
            )
        if request.url.path == "/llms-full.txt":
            return httpx.Response(404)
        raise AssertionError(request.url.path)

    _SEED_CACHE.clear()
    client = make_client(handler)
    seeder = UrlSeeder(client, robots=None)  # type: ignore[arg-type]
    result = run(seeder.discover("https://docs.example.com/"))
    assert result.urls == [
        "https://docs.example.com/api",
        "https://docs.example.com/basics",
    ]
