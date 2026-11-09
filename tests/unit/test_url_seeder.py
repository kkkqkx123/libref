"""UrlSeeder tests: local fixtures (httpx MockTransport, no network).

Golden samples: llms.txt format follows the llmstxt.org spec; sitemap structure follows the sitemap.org protocol.
"""

from __future__ import annotations

import gzip
import sqlite3

import httpx

from libref.crawler import RobotsChecker, UrlSeeder


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    import asyncio
    return asyncio.run(coro)


def test_llms_txt_preferred_over_sitemap():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/llms.txt":
            return httpx.Response(200, text="- [Install](/install.html): how to install\n")
        raise AssertionError("sitemap should not be fetched when llms.txt exists")

    client = make_client(handler)
    seeder = UrlSeeder(client, robots=None)  # type: ignore[arg-type]
    result = run(seeder._from_llms_txt("https://docs.example.com"))
    assert result.source == "llms.txt"
    assert result.urls == ["https://docs.example.com/install.html"]


def test_sitemap_gz_and_index():
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
            return httpx.Response(200, text=(
                "<sitemapindex><sitemap><loc>https://d.example/sub.xml.gz</loc></sitemap></sitemapindex>"
            ))
        if request.url.path == "/sub.xml.gz":
            return httpx.Response(200, content=child_sitemap)
        raise AssertionError(request.url.path)

    client = make_client(handler)
    seeder = UrlSeeder(client, robots=None)  # type: ignore[arg-type]
    result = run(seeder._from_sitemap("https://d.example"))
    assert result.source == "sitemap"
    assert result.urls == ["https://d.example/a.html", "https://d.example/b.html"]


def test_discover_filters_and_dedup():
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/llms.txt":
            return httpx.Response(200, text=(
                "- [A](https://docs.example.com/a.html)\n"
                "- [Dup](https://docs.example.com/a.html)\n"
                "- [Secret](https://docs.example.com/private.html)\n"
                "- [Offsite](https://other.example.com/x.html)\n"
            ))
        if p == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        raise AssertionError(request.url.path)

    client = make_client(handler)
    conn = sqlite3.connect(":memory:")
    robots = RobotsChecker(conn, client)
    seeder = UrlSeeder(client, robots)
    result = run(seeder.discover("https://docs.example.com/"))
    assert result.urls == ["https://docs.example.com/a.html"]
    conn.close()


def test_discover_head_prefilter():
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/llms.txt":
            return httpx.Response(200, text=(
                "- [A](https://docs.example.com/a.html)\n"
                "- [B](https://docs.example.com/missing.html)\n"
            ))
        if p == "/robots.txt":
            return httpx.Response(200, text="")
        if p == "/a.html" and request.method == "HEAD":
            return httpx.Response(200, headers={"content-type": "text/html"})
        if p == "/missing.html" and request.method == "HEAD":
            return httpx.Response(404)
        raise AssertionError(request.url.path)

    client = make_client(handler)
    conn = sqlite3.connect(":memory:")
    robots = RobotsChecker(conn, client)
    seeder = UrlSeeder(client, robots)
    result = run(seeder.discover("https://docs.example.com/", prefilter=True))
    assert result.urls == ["https://docs.example.com/a.html"]
    conn.close()
