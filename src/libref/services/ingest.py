"""抓取编排：UrlSeeder→Robots→Fetcher→extract→split→DocumentStore。

services 层是唯一编排点（module-layout §2）；出口层只调本模块。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from libref.chunking import split_markdown
from libref.crawler import HttpFetcher, RobotsChecker, UrlSeeder, extract_to_markdown
from libref.services.jobs import Job, JobManager
from libref.services.search import SearchService
from libref.store.document_store import DocumentStore

_DEFAULT_SEED_HOST_TTL = 7.0

SCRAPE_JOB_KIND = "scrape_docs"
"""jobs 表中抓取任务的 kind 协议（params 见 scrape_job_params）。"""


def scrape_job_params(
    library: str,
    version: str,
    start_url: str,
    *,
    max_pages: int | None = None,
    prefilter: bool = False,
) -> dict[str, Any]:
    """构造抓取任务 params（jobs 表 kind/scrape_docs 协议）。"""
    return {
        "library": library,
        "version": version,
        "start_url": start_url,
        "max_pages": max_pages,
        "prefilter": prefilter,
    }


def start_scrape_job(
    mgr: JobManager,
    library: str,
    version: str,
    start_url: str,
    *,
    max_pages: int | None = None,
    prefilter: bool = False,
) -> int:
    """提交抓取任务到 jobs 队列，返回 job_id（执行由 worker + runner 完成）。"""
    return mgr.submit(
        SCRAPE_JOB_KIND,
        scrape_job_params(library, version, start_url,
                          max_pages=max_pages, prefilter=prefilter),
    )


def build_scrape_runner(settings, conn):
    """构造 jobs worker 可用的 runner：按 job.params 执行整站抓取。"""
    service = IngestService(settings, conn)

    async def runner(job: Job) -> None:
        if job.kind != SCRAPE_JOB_KIND:
            raise ValueError(f"unsupported job kind: {job.kind}")
        params = job.params
        await service.scrape_docs_async(
            params["library"],
            params["version"],
            params["start_url"],
            max_pages=params.get("max_pages"),
            prefilter=params.get("prefilter", False),
        )

    return runner


def run_scrape_job_sync(mgr: JobManager, settings, conn, job_id: int,
                        *, timeout: float = 300.0) -> Job:
    """同步驱动单任务：起 worker → 等待落定 → 停 worker（供 CLI --async 使用）。"""
    async def _drive() -> Job:
        mgr.start_worker(build_scrape_runner(settings, conn))
        try:
            return await mgr.wait_for(job_id, timeout=timeout)
        finally:
            mgr.stop_worker()

    return asyncio.run(_drive())


@asynccontextmanager
async def _client(settings) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        timeout=settings.request_timeout,
        follow_redirects=True,
        headers={"User-Agent": getattr(settings, "user_agent", "libref-crawler/0.1")},
        http2=True,
    ) as client:
        yield client


class IngestService:
    def __init__(self, settings, conn) -> None:
        self._settings = settings
        self._conn = conn

    def scrape_docs(
        self,
        library: str,
        version: str,
        start_url: str,
        *,
        max_pages: int | None = None,
        prefilter: bool = False,
    ) -> dict[str, int]:
        """同步入口：发现→抓取→净化→分块→落库，返回六态计数。"""
        return asyncio.run(self.scrape_docs_async(library, version, start_url,
                                                  max_pages=max_pages, prefilter=prefilter))

    async def scrape_docs_async(
        self,
        library: str,
        version: str,
        start_url: str,
        *,
        max_pages: int | None = None,
        prefilter: bool = False,
    ) -> dict[str, int]:
        """异步入口（jobs worker 复用，避免在运行中事件循环内套 asyncio.run）。"""
        return await self._scrape_async(library, version, start_url,
                                        max_pages=max_pages, prefilter=prefilter)

    async def _scrape_async(
        self,
        library: str,
        version: str,
        start_url: str,
        *,
        max_pages: int | None,
        prefilter: bool,
    ) -> dict[str, int]:
        counts = {k: 0 for k in
                  ("stored", "unchanged", "absent", "empty", "skipped", "failed")}
        async with _client(self._settings) as client:
            robots = RobotsChecker(
                self._conn, client,
                ttl_days=getattr(self._settings, "robots_cache_ttl_days", _DEFAULT_SEED_HOST_TTL),
                user_agent=getattr(self._settings, "user_agent", "libref-crawler/0.1"),
            )
            seeder = UrlSeeder(client, robots)
            fetcher = HttpFetcher(
                client,
                max_retries=getattr(self._settings, "fetch_max_retries", 3),
                default_delay=getattr(self._settings, "crawl_delay_default", 1.0),
            )
            seed = await seeder.discover(start_url, prefilter=prefilter, max_pages=max_pages)

            store = DocumentStore(self._conn)
            search = SearchService(self._conn)
            library_id, version_id = search.ensure_library_version(library, version)

            sem = asyncio.Semaphore(getattr(self._settings, "fetch_concurrency", 4))

            async def process(url: str) -> None:
                async with sem:
                    page = store.get_page(version_id, url)
                    outcome = await self._fetch_and_store(
                        fetcher, robots, store, library_id, version_id, url, page
                    )
                    counts[outcome] = counts.get(outcome, 0) + 1

            await asyncio.gather(*(process(u) for u in seed.urls))
            self._sweep_absent(store, library_id, version_id, seed,
                               max_pages=max_pages, counts=counts)
        return counts

    def _sweep_absent(self, store: DocumentStore, library_id: int, version_id: int,
                      seed, *, max_pages: int | None,
                      counts: dict[str, int]) -> None:
        """缺席清理：已有 pages − 本次发现集 → absent。

        发现截断（max_pages 封顶）或发现源为空（全部探测失败）时跳过，
        避免把未覆盖/瞬时失败的页面误删。
        """
        if seed.source == "none":
            return
        if max_pages is not None and len(seed.urls) >= max_pages:
            return
        discovered = set(seed.urls)
        for url in store.list_page_urls(version_id):
            if url not in discovered:
                store.store_page(version_id, library_id, url, "absent")
                counts["absent"] = counts.get("absent", 0) + 1

    async def _fetch_and_store(
        self,
        fetcher: HttpFetcher,
        robots: RobotsChecker,
        store: DocumentStore,
        library_id: int,
        version_id: int,
        url: str,
        page,
    ) -> str:
        if not await robots.can_fetch(url):
            return "skipped"
        delay = await robots.crawl_delay(url)
        result = await fetcher.fetch(
            url, etag=page.etag if page else None,
            last_modified=page.last_modified if page else None, crawl_delay=delay,
        )
        if result.status == "unchanged":
            return store.store_page(version_id, library_id, url, "unchanged")
        if result.status == "absent":
            return store.store_page(version_id, library_id, url, "absent")
        if result.status == "failed":
            return store.store_page(version_id, library_id, url, "failed")

        markdown = extract_to_markdown(result.content or "", url)
        if not markdown:
            return store.store_page(
                version_id, library_id, url, "empty",
                raw_content=result.content, etag=result.etag, last_modified=result.last_modified,
            )
        outcome = store.ensure_changed(version_id, url, markdown)
        if outcome == "unchanged":
            return store.store_page(version_id, library_id, url, "unchanged")
        chunks = split_markdown(markdown, url)
        return store.store_page(
            version_id, library_id, url, "stored",
            chunks=chunks, raw_content=markdown,
            etag=result.etag, last_modified=result.last_modified,
        )
