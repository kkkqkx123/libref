"""services 层测试：search_docs bm25 命中、jobs 队列状态流转、ingest 编排（MockTransport）。"""

from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest

from libref.chunking import Chunk
from libref.config import LibrefSettings
from libref.services import (
    IngestService,
    JobManager,
    SearchService,
    build_scrape_runner,
    run_scrape_job_sync,
    start_scrape_job,
)
from libref.store.document_store import DocumentStore
from tests.conftest import mock_ingest, seed_library_version

pytestmark = pytest.mark.integration


def seed_lv(conn, library: str = "docs", version: str = "1.0") -> tuple[int, int]:
    return seed_library_version(conn, library, version)


def seed_docs(conn: sqlite3.Connection) -> None:
    seed_lv(conn)
    store = DocumentStore(conn)
    store.store_page(
        1, 1, "https://d.example/a.html", "stored",
        chunks=[Chunk(url="https://d.example/a.html", path="Guide", title="Guide",
                      content="libref uses uv for dependency management", sort_order=0)],
        raw_content="body",
    )
    store.store_page(
        1, 1, "https://d.example/b.html", "stored",
        chunks=[Chunk(url="https://d.example/b.html", path="API", title="API",
                      content="unrelated pineapple text", sort_order=0)],
        raw_content="body2",
    )


def test_search_docs_bm25_hits_and_snippet(conn):
    seed_docs(conn)
    results = SearchService(conn).search_docs("libref")
    assert len(results) == 1
    assert results[0]["url"] == "https://d.example/a.html"
    assert "libref" in results[0]["snippet"]


def test_search_docs_library_version_filter(conn):
    seed_docs(conn)
    svc = SearchService(conn)
    assert svc.search_docs("libref", library="nosuch") == []
    assert svc.search_docs("libref", version="1.0") != []
    conn.execute("INSERT INTO libraries(name) VALUES('other')")
    conn.execute("INSERT INTO versions(library_id, version) VALUES(2, '9.9')")
    conn.commit()


def test_read_page_reassembles(conn):
    seed_lv(conn)
    store = DocumentStore(conn)
    store.store_page(1, 1, "https://d.example/p.html", "stored", chunks=[
        Chunk(url="https://d.example/p.html", path="A", title="A", content="part one", sort_order=0),
        Chunk(url="https://d.example/p.html", path="A / B", title="B", content="part two", sort_order=1),
    ], raw_content="x")
    page = SearchService(conn).read_page("https://d.example/p.html")
    assert page is not None
    assert page["content"] == "part one\n\npart two"
    assert page["chunks"] == 2


def test_list_libraries_and_versions(conn):
    svc = SearchService(conn)
    svc.ensure_library_version("docs", "1.0")
    svc.ensure_library_version("docs", "2.0")
    libs = svc.list_libraries()
    assert libs[0]["name"] == "docs" and libs[0]["versions"] == 2
    versions = svc.list_versions("docs")
    assert {v["version"] for v in versions} == {"1.0", "2.0"}


def test_jobs_submit_run_and_recover(conn):
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

    # stale 恢复：伪造 running 且心跳过期
    conn.execute("UPDATE jobs SET status='running', heartbeat_at=0 WHERE id=?", (job_id,))
    conn.commit()
    stale = mgr.recover()
    assert stale == [job_id]
    assert mgr.get(job_id).status == "failed"


def test_jobs_failure_recorded(conn):
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


def test_scrape_docs_end_to_end_with_mock(conn, tmp_path):
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
            return httpx.Response(200, text=(
                f"- [Install](https://{host}/install.html)\n"
                f"- [Missing](https://{host}/gone.html)\n"
            ))
        if p == "/install.html":
            return httpx.Response(200, text=page_html)
        if p == "/gone.html":
            return httpx.Response(404)
        raise AssertionError(request.url.path)

    settings = LibrefSettings(
        request_timeout=5.0, fetch_concurrency=2, crawl_delay_default=0.0,
        robots_cache_ttl_days=1.0,
    )
    service = IngestService(settings, conn)

    with mock_ingest(handler):
        counts = service.scrape_docs("docs", "1.0", "https://d.example/", max_pages=5)

    assert counts["stored"] == 1
    assert counts["absent"] == 1
    results = SearchService(conn).search_docs("libref")
    assert len(results) == 1
    assert results[0]["url"] == "https://d.example/install.html"


def test_scrape_sweeps_pages_missing_from_seed_to_absent(conn):
    seed_lv(conn)
    store = DocumentStore(conn)
    keep_url = "https://d.example/keep.html"
    stale_url = "https://d.example/stale.html"
    for url, body in ((keep_url, "keep body quetzalkeep"),
                      (stale_url, "stale body quetzalstale")):
        store.store_page(
            1, 1, url, "stored",
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
                200, text="<html><body><main><p>keep body quetzalkeep</p></main></body></html>")
        raise AssertionError(request.url.path)

    settings = LibrefSettings(
        request_timeout=5.0, fetch_concurrency=2, crawl_delay_default=0.0,
        robots_cache_ttl_days=1.0,
    )
    with mock_ingest(handler):
        counts = IngestService(settings, conn).scrape_docs("docs", "1.0", "https://d.example/")

    assert counts["absent"] == 1
    assert store.get_page(1, stale_url) is None
    assert store.get_page(1, keep_url) is not None
    assert SearchService(conn).search_docs("quetzalstale") == []


def test_scrape_skips_sweep_when_seed_truncated(conn):
    seed_lv(conn)
    store = DocumentStore(conn)
    other_url = "https://d.example/other.html"
    store.store_page(
        1, 1, other_url, "stored",
        chunks=[Chunk(url=other_url, path="G", title="G", content="other body", sort_order=0)],
        raw_content="other body",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path == "/llms.txt":
            return httpx.Response(200, text=(
                "- [A](https://d.example/a.html)\n"
                "- [B](https://d.example/b.html)\n"
            ))
        if request.url.path in ("/a.html", "/b.html"):
            return httpx.Response(
                200, text="<html><body><main><p>page body</p></main></body></html>")
        raise AssertionError(request.url.path)

    settings = LibrefSettings(
        request_timeout=5.0, fetch_concurrency=2, crawl_delay_default=0.0,
        robots_cache_ttl_days=1.0,
    )
    with mock_ingest(handler):
        # max_pages=1 截断发现集：缺席清理必须跳过，避免误删未覆盖页面
        IngestService(settings, conn).scrape_docs(
            "docs", "1.0", "https://d.example/", max_pages=1)

    assert store.get_page(1, other_url) is not None


def test_scrape_falls_back_to_start_url_without_seed(conn):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path == "/javadoc/index.html":
            return httpx.Response(
                200, text="<html><body><main><p>javadoc fallback body</p></main></body></html>")
        return httpx.Response(404)

    settings = LibrefSettings(
        request_timeout=5.0, fetch_concurrency=2, crawl_delay_default=0.0,
        robots_cache_ttl_days=1.0,
    )
    with mock_ingest(handler):
        counts = IngestService(settings, conn).scrape_docs(
            "docs", "1.0", "https://d.example/javadoc/index.html")
    assert counts["stored"] == 1
    assert SearchService(conn).search_docs("javadoc") != []


def test_start_scrape_job_runs_through_queue(conn):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path == "/llms.txt":
            return httpx.Response(200, text="- [Install](https://d.example/install.html)\n")
        if request.url.path == "/install.html":
            return httpx.Response(
                200, text="<html><body><main><p>use uv to install libref</p></main></body></html>")
        raise AssertionError(request.url.path)

    settings = LibrefSettings(
        request_timeout=5.0, fetch_concurrency=2, crawl_delay_default=0.0,
        robots_cache_ttl_days=1.0,
    )
    mgr = JobManager(conn)
    assert mgr.recover() == []
    job_id = start_scrape_job(mgr, "docs", "1.0", "https://d.example/", max_pages=5)
    assert mgr.get(job_id).kind == "scrape_docs"

    with mock_ingest(handler):
        job = run_scrape_job_sync(mgr, settings, conn, job_id, timeout=30.0)

    assert job.status == "done"
    assert SearchService(conn).search_docs("libref") != []


def test_scrape_runner_rejects_unknown_kind(conn):
    settings = LibrefSettings(crawl_delay_default=0.0)
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
