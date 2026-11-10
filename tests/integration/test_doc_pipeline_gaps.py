"""Doc pipeline gap regressions: dispatch, windowing, observability, heartbeat."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from libref.chunking import split_markdown_dual
from libref.config import LibrefSettings
from libref.services.ingest import IngestService
from libref.services.jobs import JobManager
from libref.services.pages import PageService
from libref.services.search import SearchService
from libref.text.normalize import classify_content
from tests.conftest import mock_ingest

pytestmark = pytest.mark.integration


def test_classify_content_dispatch() -> None:
    assert classify_content("text/html", "https://h/a") == "html"
    assert classify_content("text/markdown", "https://h/a") == "markdown"
    assert classify_content("text/plain", "https://h/a") == "text"
    assert classify_content(None, "https://h/a.md") == "markdown"
    assert classify_content(None, "https://h/a.py") == "source"
    assert classify_content("application/javascript", "https://h/a") == "source"


def test_chunk_relations_keywords_offsets() -> None:
    dual = split_markdown_dual(
        "# A\n\nfirst body here\n\n# B\n\nsecond body here\n", "u"
    )
    assert dual.embedding, "embedding stream is empty"
    first = dual.embedding[0]
    assert first.content_hash, "chunk hash is empty"
    assert first.keywords, "keywords are empty"
    assert first.start_char >= 0 and first.end_char > first.start_char
    if len(dual.embedding) > 1:
        assert dual.embedding[0].next_chunk_id == dual.embedding[1].chunk_id
        assert dual.embedding[1].prev_chunk_id == dual.embedding[0].chunk_id


def test_scrape_mime_markdown_and_observability(conn) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(("llms.txt", "llms-full.txt")):
            return httpx.Response(404)
        if path == "/robots.txt":
            return httpx.Response(200, text="")
        if path == "/sitemap.xml":
            host = request.url.host
            return httpx.Response(
                200,
                text=(
                    '<?xml version="1.0"?><urlset>'
                    f"<url><loc>https://{host}/b.md</loc></url></urlset>"
                ),
            )
        if path == "/b.md":
            return httpx.Response(
                200,
                text="# G\n\nsmoke markdown body\n",
                headers={"Content-Type": "text/markdown"},
            )
        if "sitemap" in path:
            return httpx.Response(404)
        raise AssertionError(path)

    settings = LibrefSettings(crawl_delay_default=0.0, fetch_concurrency=2)
    with mock_ingest(handler):
        counts = IngestService(settings, conn).scrape_docs(
            "docs", "1.0", "https://d.example/sitemap.xml"
        )
    assert counts["stored"] == 1
    for key in (
        "seed_discovered",
        "scope_rejected",
        "seed_truncated",
        "elapsed_ms",
        "chunk_reused",
        "render_skipped",
    ):
        assert key in counts, key
    hits = SearchService(conn).search_docs("smoke")
    assert hits and hits[0]["url"].endswith("/b.md")


def test_refresh_alias_and_window(conn) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(("llms.txt", "llms-full.txt")):
            return httpx.Response(404)
        if path == "/robots.txt":
            return httpx.Response(200, text="")
        if path in ("/", "/sitemap.xml"):
            host = request.url.host
            return httpx.Response(
                200,
                text=(
                    '<?xml version="1.0"?><urlset>'
                    f"<url><loc>https://{host}/a.html</loc></url></urlset>"
                ),
            )
        if path == "/a.html":
            return httpx.Response(
                200,
                text=(
                    "<html><body><main><h1>T</h1><p>refresh body one</p>"
                    "<p>refresh body two</p></main></body></html>"
                ),
            )
        if "sitemap" in path:
            return httpx.Response(404)
        raise AssertionError(path)

    settings = LibrefSettings(crawl_delay_default=0.0, fetch_concurrency=1)
    with mock_ingest(handler):
        service = IngestService(settings, conn)
        counts = service.refresh_version("docs", "1.0", "https://d.example/")
    assert counts["stored"] == 1
    window = PageService(conn).read_window("https://d.example/a.html", 0, radius=1)
    assert window is not None and window["docs"]
    section = PageService(conn).read_section(
        "https://d.example/a.html",
        window["docs"][0].get("source_group_id", ""),
    )
    assert section is None or section["chunks"] >= 1


def test_job_heartbeat_ticks(conn) -> None:
    seen: list[float] = []

    async def runner(job):
        import time as _time

        for _ in range(5):
            await asyncio.sleep(0.05)
            seen.append(_time.time())

    async def scenario():
        mgr = JobManager(conn, stale_after=600.0)
        job_id = mgr.submit("k", {})
        mgr.start_worker(runner)
        for _ in range(100):
            job = mgr.get(job_id)
            if job.status in ("done", "failed"):
                break
            await asyncio.sleep(0.02)
        mgr.stop_worker()
        return mgr.get(job_id)

    job = asyncio.run(scenario())
    assert job.status == "done"
    assert len(seen) == 5
    row = conn.execute("SELECT heartbeat_at FROM jobs WHERE id=?", (job.id,)).fetchone()
    assert row[0] is not None


def test_render_skipped_counted(conn) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(("llms.txt", "llms-full.txt")):
            return httpx.Response(404)
        if path == "/robots.txt":
            return httpx.Response(200, text="")
        if path in ("/", "/sitemap.xml"):
            host = request.url.host
            return httpx.Response(
                200,
                text=(
                    '<?xml version="1.0"?><urlset>'
                    f"<url><loc>https://{host}/app.html</loc></url></urlset>"
                ),
            )
        if path == "/app.html":
            return httpx.Response(
                200,
                text=(
                    '<html><body><div id="root"></div>'
                    "<script>var x=1;</script></body></html>"
                ),
            )
        if "sitemap" in path:
            return httpx.Response(404)
        raise AssertionError(path)

    settings = LibrefSettings(crawl_delay_default=0.0, fetch_concurrency=1)
    with mock_ingest(handler):
        counts = IngestService(settings, conn).scrape_docs(
            "docs", "1.0", "https://d.example/"
        )
    assert counts["skipped"] >= 1
    assert counts["render_skipped"] >= 1
