"""Scrape audit, snapshot, and retention governance regressions."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from libref.config import LibrefSettings
from libref.services.code_ingest import index_repo
from libref.services.ingest import run_scrape_job_sync, start_scrape_job
from libref.services.jobs import JobManager
from libref.services.retention import (
    list_deletions,
    log_deletion,
    prune_expired_logs,
    purge_version,
)
from libref.services.search import SearchService
from libref.store.snapshots import SnapshotStore
from tests.conftest import copy_mini_repo, mock_ingest

pytestmark = pytest.mark.integration


def _handler(request: httpx.Request) -> httpx.Response:
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
                f"<url><loc>https://{host}/a.html</loc></url>"
                f"<url><loc>https://{host}/b.md</loc></url></urlset>"
            ),
        )
    if path == "/a.html":
        return httpx.Response(
            200,
            text="<html><body><main><h1>T</h1><p>audit body one</p></main></body></html>",
        )
    if path == "/b.md":
        return httpx.Response(200, text="# T\n\naudit body two\n")
    if "sitemap" in path:
        return httpx.Response(404)
    raise AssertionError(path)


def test_async_scrape_persists_result(conn) -> None:
    settings = LibrefSettings(crawl_delay_default=0.0, fetch_concurrency=2)
    mgr = JobManager(conn)
    job_id = start_scrape_job(mgr, "docs", "1.0", "https://d.example/sitemap.xml")
    with mock_ingest(_handler):
        job = run_scrape_job_sync(mgr, settings, conn, job_id, timeout=30.0)
    assert job.status == "done"
    assert isinstance(job.result, dict)
    assert job.result.get("stored", 0) >= 1
    assert job.finished_at is not None
    fresh = mgr.get(job_id)
    assert fresh is not None and fresh.result.get("stored", 0) >= 1


def test_repo_runner_persists_counts(conn, tmp_path) -> None:
    from libref.services.code_ingest import INDEX_REPO_JOB_KIND, build_repo_runner

    repo = copy_mini_repo(tmp_path)
    settings = LibrefSettings()
    mgr = JobManager(conn)
    job_id = mgr.submit(
        INDEX_REPO_JOB_KIND,
        {
            "library": "demo",
            "version": "0.1",
            "source": str(repo),
            "ecosystem": "custom",
        },
    )

    async def scenario():
        mgr.start_worker(build_repo_runner(settings, conn, tmp_path / "repos"))
        try:
            return await mgr.wait_for(job_id, timeout=60.0)
        finally:
            mgr.stop_worker()

    job = asyncio.run(scenario())
    assert job.status == "done"
    assert job.result.get("calls", 0) >= 1


def test_snapshot_written_when_enabled(conn, tmp_path) -> None:
    settings = LibrefSettings(
        crawl_delay_default=0.0,
        fetch_concurrency=2,
        snapshot_enabled=True,
        snapshot_dir=tmp_path / "snaps",
        snapshot_quota_mb=64,
    )
    mgr = JobManager(conn)
    job_id = start_scrape_job(mgr, "docs", "1.0", "https://d.example/sitemap.xml")
    with mock_ingest(_handler):
        job = run_scrape_job_sync(mgr, settings, conn, job_id, timeout=30.0)
    assert job.status == "done"
    assert job.result.get("snapshot_written", 0) >= 1
    store = SnapshotStore(tmp_path / "snaps")
    assert len(store.version_files("custom", "docs", "1.0")) >= 1


def test_snapshot_disabled_by_default(conn) -> None:
    settings = LibrefSettings(crawl_delay_default=0.0, fetch_concurrency=2)
    assert settings.snapshot_enabled is False
    mgr = JobManager(conn)
    job_id = start_scrape_job(mgr, "docs", "1.0", "https://d.example/sitemap.xml")
    with mock_ingest(_handler):
        job = run_scrape_job_sync(mgr, settings, conn, job_id, timeout=30.0)
    assert job.status == "done"
    assert job.result.get("snapshot_written", 0) == 0


def test_absent_sweep_logs_deletion(conn) -> None:
    settings = LibrefSettings(crawl_delay_default=0.0, fetch_concurrency=2)
    with mock_ingest(_handler):
        from libref.services.ingest import IngestService

        first = IngestService(settings, conn).scrape_docs(
            "docs", "1.0", "https://d.example/sitemap.xml"
        )
    assert first["stored"] >= 1

    def _gone(request: httpx.Request) -> httpx.Response:
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
                    f"<url><loc>https://{host}/a.html</loc></url></urlset>"
                ),
            )
        if path == "/a.html":
            return httpx.Response(
                200,
                text="<html><body><main><p>audit body one</p></main></body></html>",
            )
        if "sitemap" in path:
            return httpx.Response(404)
        raise AssertionError(path)

    with mock_ingest(_gone):
        from libref.services.ingest import IngestService

        second = IngestService(settings, conn).scrape_docs(
            "docs", "1.0", "https://d.example/sitemap.xml"
        )
    assert second["absent"] >= 1
    rows = list_deletions(conn)
    assert any(r["reason"] == "absent-sweep" for r in rows)


def test_purge_version_clears_and_logs(conn, tmp_path) -> None:
    repo = copy_mini_repo(tmp_path)
    counts = index_repo(conn, "demo", "0.1", str(repo), repos_dir=tmp_path / "repos")
    assert counts["calls"] >= 1
    out = purge_version(
        conn,
        "demo",
        "0.1",
        repos_dir=tmp_path / "repos",
        snapshot_root=tmp_path / "snaps",
    )
    assert out["removed_docs"] >= 1
    assert SearchService(conn).search_docs("numpy") == []
    assert conn.execute("SELECT count(*) FROM call_instances").fetchone()[0] == 0
    rows = list_deletions(conn)
    assert any(r["reason"] == "purge" for r in rows)


def test_deletion_log_prune(conn) -> None:
    log_deletion(conn, library="docs", version="9.9", reason="purge", detail={})
    assert prune_expired_logs(conn, ttl_days=30.0) == 0
    assert prune_expired_logs(conn, ttl_days=-1) >= 1
