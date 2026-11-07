"""services 层测试：search_docs bm25 命中、jobs 队列状态流转、ingest 编排（MockTransport）。"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3

import httpx

from libref.chunking import Chunk
from libref.config import LibrefSettings
from libref.services import IngestService, JobManager, SearchService
from libref.store.document_store import DocumentStore


def seed_lv(conn, library: str = "docs", version: str = "1.0") -> tuple[int, int]:
    """测试库/版本外键行（固定 id=1）。"""
    conn.execute("INSERT INTO libraries(id, name) VALUES(1, ?)", (library,))
    conn.execute("INSERT INTO versions(id, library_id, version) VALUES(1, 1, ?)", (version,))
    conn.commit()
    return 1, 1


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

    # 补丁 ingest 的 _client 上下文管理器，注入 MockTransport（不联网，不递归）
    mock_client = make_client(handler)

    @contextlib.asynccontextmanager
    async def fake_client(_settings):
        yield mock_client

    import libref.services.ingest as ingest_mod
    original = ingest_mod._client
    ingest_mod._client = fake_client
    try:
        counts = service.scrape_docs("docs", "1.0", "https://d.example/", max_pages=5)
    finally:
        ingest_mod._client = original
        import asyncio
        asyncio.run(mock_client.aclose())

    assert counts["stored"] == 1
    assert counts["absent"] == 1
    results = SearchService(conn).search_docs("libref")
    assert len(results) == 1
    assert results[0]["url"] == "https://d.example/install.html"


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))
