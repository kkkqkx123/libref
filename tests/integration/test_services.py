"""services layer tests: search_docs bm25 hits, jobs queue state transitions, ingest orchestration (MockTransport)."""

from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest

from libref.chunking import Chunk
from libref.crawler.url_seeder import _SEED_CACHE
from libref.services import (
    IngestService,
    JobManager,
    SearchService,
    build_scrape_runner,
    run_scrape_job_sync,
    start_scrape_job,
)
from libref.store.document_store import DocumentStore
from libref.text.normalize import classify_content
from tests.conftest import make_crawler_settings, mock_ingest, seed_library_version

pytestmark = pytest.mark.integration


def seed_lv(conn, library: str = "docs", version: str = "1.0") -> tuple[int, int]:
    return seed_library_version(conn, library, version)


def seed_docs(conn: sqlite3.Connection) -> None:
    seed_lv(conn)
    store = DocumentStore(conn)
    store.store_page(
        1,
        1,
        "https://d.example/a.html",
        "stored",
        chunks=[
            Chunk(
                url="https://d.example/a.html",
                path="Guide",
                title="Guide",
                content="libref uses uv for dependency management",
                sort_order=0,
            )
        ],
        raw_content="body",
    )
    store.store_page(
        1,
        1,
        "https://d.example/b.html",
        "stored",
        chunks=[
            Chunk(
                url="https://d.example/b.html",
                path="API",
                title="API",
                content="unrelated pineapple text",
                sort_order=0,
            )
        ],
        raw_content="body2",
    )


def test_search_docs_bm25_hits_and_snippet(conn, monkeypatch, tmp_path):
    seed_docs(conn)
    results = SearchService(conn).search_docs("libref")
    assert len(results) == 1
    assert results[0]["url"] == "https://d.example/a.html"
    assert "libref" in results[0]["snippet"]
    assert "<hit>libref</hit>" in results[0]["snippet"]
    assert ">>>" not in results[0]["snippet"]


def test_search_docs_library_version_filter(conn, monkeypatch, tmp_path):
    seed_docs(conn)
    svc = SearchService(conn)
    assert svc.search_docs("libref", library="nosuch") == []
    assert svc.search_docs("libref", version="1.0") != []
    conn.execute("INSERT INTO libraries(name) VALUES('other')")
    conn.execute("INSERT INTO versions(library_id, version) VALUES(2, '9.9')")
    conn.commit()


def test_read_page_reassembles(conn, monkeypatch, tmp_path):
    seed_lv(conn)
    store = DocumentStore(conn)
    store.store_page(
        1,
        1,
        "https://d.example/p.html",
        "stored",
        chunks=[
            Chunk(
                url="https://d.example/p.html",
                path="A",
                title="A",
                content="part one",
                sort_order=0,
            ),
            Chunk(
                url="https://d.example/p.html",
                path="A / B",
                title="B",
                content="part two",
                sort_order=1,
            ),
        ],
        raw_content="x",
    )
    from libref.services.docs.pages import PageService

    page = PageService(conn).read_page("https://d.example/p.html")
    assert page is not None
    assert "part one" in page["content"]
    assert "part two" in page["content"]
    assert "<unit" in page["content"] and "</unit>" in page["content"]
    assert page["chunks"] == 2
    assert all("content" not in item for item in page["chunk_details"])
    assert page["chunk_details"][0]["length"] == len("part one")


def test_list_libraries_and_versions(conn, monkeypatch, tmp_path):
    from libref.services.registry.libraries import LibraryService

    svc = LibraryService(conn)
    svc.ensure_library_version("docs", "1.0")
    svc.ensure_library_version("docs", "2.0")
    libs = svc.list_libraries()
    assert libs[0]["name"] == "docs" and libs[0]["versions"] == 2
    versions = svc.list_versions("docs")
    assert {v["version"] for v in versions} == {"1.0", "2.0"}


def test_jobs_submit_run_and_recover(conn, monkeypatch, tmp_path):
    ran: list[int] = []

    def runner(job):
        ran.append(job.id)

    async def scenario():
        mgr = JobManager(conn, stale_after=600.0)
        job_id = mgr.submit("ingest", {"url": "https://d.example"})
        mgr.start_worker(runner)
        for _ in range(50):
            row = mgr.get(job_id)
            if row.status == "done":
                break
            await asyncio.sleep(0.02)
        mgr.stop_worker()
        return mgr, job_id

    mgr, job_id = asyncio.run(scenario())
    assert ran == [job_id]
    assert mgr.get(job_id).status == "done"

    # stale recovery: forge a running job with an expired heartbeat
    conn.execute(
        "UPDATE jobs SET status='running', heartbeat_at=0 WHERE id=?", (job_id,)
    )
    conn.commit()
    stale = mgr.recover()
    assert stale == [job_id]
    assert mgr.get(job_id).status == "failed"


def test_jobs_failure_recorded(conn, monkeypatch, tmp_path):
    def runner(job):
        raise RuntimeError("boom")

    async def scenario():
        mgr = JobManager(conn)
        job_id = mgr.submit("ingest")
        mgr.start_worker(runner)
        for _ in range(50):
            if mgr.get(job_id).status == "failed":
                break
            await asyncio.sleep(0.02)
        mgr.stop_worker()
        return mgr.get(job_id)

    job = asyncio.run(scenario())
    assert job.status == "failed"
    assert "boom" in (job.error or "")


def test_scrape_docs_end_to_end_with_mock(conn, tmp_path, monkeypatch):
    page_html = (
        "<html><head><title>Install</title></head><body>"
        "<nav>skip me</nav><main><h1>Install</h1><p>use uv to install libref</p></main>"
        "</body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        p = request.url.path
        if p == "/robots.txt":
            return httpx.Response(200, text="")
        if p == "/llms.txt":
            return httpx.Response(
                200,
                text=(
                    f"- [Install](https://{host}/install.html)\n"
                    f"- [Missing](https://{host}/gone.html)\n"
                ),
            )
        if p == "/install.html":
            return httpx.Response(200, text=page_html)
        if p == "/gone.html":
            return httpx.Response(404)
        raise AssertionError(request.url.path)

    settings = make_crawler_settings(monkeypatch, tmp_path)
    service = IngestService(settings, conn)

    with mock_ingest(handler):
        counts = service.scrape_docs("docs", "1.0", "https://d.example/", max_pages=5)

    assert counts["stored"] == 1
    assert counts["absent"] == 1
    results = SearchService(conn).search_docs("libref")
    assert len(results) == 1
    assert results[0]["url"] == "https://d.example/install.html"


def test_scrape_sweeps_pages_missing_from_seed_to_absent(conn, monkeypatch, tmp_path):
    seed_lv(conn)
    store = DocumentStore(conn)
    keep_url = "https://d.example/keep.html"
    stale_url = "https://d.example/stale.html"
    for url, body in (
        (keep_url, "keep body quetzalkeep"),
        (stale_url, "stale body quetzalstale"),
    ):
        store.store_page(
            1,
            1,
            url,
            "stored",
            chunks=[Chunk(url=url, path="G", title="G", content=body, sort_order=0)],
            raw_content=body,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path == "/llms.txt":
            return httpx.Response(200, text=f"- [Keep]({keep_url})\n")
        if request.url.path == "/keep.html":
            return httpx.Response(
                200,
                text="<html><body><main><p>keep body quetzalkeep</p></main></body></html>",
            )
        raise AssertionError(request.url.path)

    settings = make_crawler_settings(monkeypatch, tmp_path)
    with mock_ingest(handler):
        counts = IngestService(settings, conn).scrape_docs(
            "docs", "1.0", "https://d.example/"
        )

    assert counts["absent"] == 1
    assert store.get_page(1, stale_url) is None
    assert store.get_page(1, keep_url) is not None
    assert SearchService(conn).search_docs("quetzalstale") == []


def test_scrape_skips_sweep_when_seed_truncated(conn, monkeypatch, tmp_path):
    seed_lv(conn)
    store = DocumentStore(conn)
    other_url = "https://d.example/other.html"
    store.store_page(
        1,
        1,
        other_url,
        "stored",
        chunks=[
            Chunk(
                url=other_url, path="G", title="G", content="other body", sort_order=0
            )
        ],
        raw_content="other body",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path == "/llms.txt":
            return httpx.Response(
                200,
                text=(
                    "- [A](https://d.example/a.html)\n- [B](https://d.example/b.html)\n"
                ),
            )
        if request.url.path in ("/a.html", "/b.html"):
            return httpx.Response(
                200, text="<html><body><main><p>page body</p></main></body></html>"
            )
        raise AssertionError(request.url.path)

    settings = make_crawler_settings(monkeypatch, tmp_path)
    with mock_ingest(handler):
        # max_pages=1 truncates the discovery set: absent-page cleanup must be skipped to avoid deleting uncovered pages
        IngestService(settings, conn).scrape_docs(
            "docs", "1.0", "https://d.example/", max_pages=1
        )

    assert store.get_page(1, other_url) is not None


def test_scrape_falls_back_to_start_url_without_seed(conn, monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path == "/javadoc/index.html":
            return httpx.Response(
                200,
                text="<html><body><main><p>javadoc fallback body</p></main></body></html>",
            )
        return httpx.Response(404)

    settings = make_crawler_settings(monkeypatch, tmp_path)
    with mock_ingest(handler):
        counts = IngestService(settings, conn).scrape_docs(
            "docs", "1.0", "https://d.example/javadoc/index.html"
        )
    assert counts["stored"] == 1
    assert SearchService(conn).search_docs("javadoc") != []


def test_start_scrape_job_runs_through_queue(conn, monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path == "/llms.txt":
            return httpx.Response(
                200, text="- [Install](https://d.example/install.html)\n"
            )
        if request.url.path == "/install.html":
            return httpx.Response(
                200,
                text="<html><body><main><p>use uv to install libref</p></main></body></html>",
            )
        raise AssertionError(request.url.path)

    settings = make_crawler_settings(monkeypatch, tmp_path)
    mgr = JobManager(conn)
    assert mgr.recover() == []
    job_id = start_scrape_job(mgr, "docs", "1.0", "https://d.example/", max_pages=5)
    assert mgr.get(job_id).kind == "scrape_docs"

    with mock_ingest(handler):
        job = run_scrape_job_sync(mgr, settings, conn, job_id, timeout=30.0)

    assert job.status == "done"
    assert SearchService(conn).search_docs("libref") != []


def test_scrape_runner_rejects_unknown_kind(conn, monkeypatch, tmp_path):
    settings = make_crawler_settings(monkeypatch, tmp_path)
    mgr = JobManager(conn)
    job_id = mgr.submit("nope", {})

    async def scenario():
        mgr.start_worker(build_scrape_runner(settings, conn))
        try:
            return await mgr.wait_for(job_id, timeout=30.0)
        finally:
            mgr.stop_worker()

    job = asyncio.run(scenario())
    assert job.status == "failed"
    assert "unsupported job kind" in (job.error or "")


def _follow_handler(request: httpx.Request) -> httpx.Response:
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
    with mock_ingest(_follow_handler):
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
    with mock_ingest(_follow_handler):
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
    with mock_ingest(_follow_handler):
        counts = IngestService(settings, conn).scrape_docs(
            "docs",
            "1.0",
            "https://d.example/",
            max_pages=1,
            scope={"follow_links": True},
        )
    assert counts["stored"] == 1
    assert counts["deep_truncated"] == 1


def test_classify_content_dispatch(monkeypatch, tmp_path) -> None:
    assert classify_content("text/html", "https://h/a") == "html"
    assert classify_content("text/markdown", "https://h/a") == "markdown"
    assert classify_content("text/plain", "https://h/a") == "text"
    assert classify_content(None, "https://h/a.md") == "markdown"
    assert classify_content(None, "https://h/a.py") == "source"
    assert classify_content("application/javascript", "https://h/a") == "source"


def test_scrape_mime_markdown_and_observability(conn, monkeypatch, tmp_path) -> None:
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

    settings = make_crawler_settings(monkeypatch, tmp_path)
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


def test_refresh_alias_and_window(conn, monkeypatch, tmp_path) -> None:
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

    settings = make_crawler_settings(monkeypatch, tmp_path, fetch_concurrency=1)
    from libref.services.docs.pages import PageService

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


def test_job_heartbeat_ticks(conn, monkeypatch, tmp_path) -> None:
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


def test_render_skipped_counted(conn, monkeypatch, tmp_path) -> None:
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

    settings = make_crawler_settings(monkeypatch, tmp_path, fetch_concurrency=1)
    with mock_ingest(handler):
        counts = IngestService(settings, conn).scrape_docs(
            "docs", "1.0", "https://d.example/"
        )
    assert counts["skipped"] >= 1
    assert counts["render_skipped"] >= 1
