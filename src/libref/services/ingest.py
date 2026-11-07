"""抓取编排：UrlSeeder→Robots→Fetcher→extract→split→DocumentStore。

services 层是唯一编排点（module-layout §2）；出口层只调本模块。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from libref.chunking import split_markdown
from libref.crawler import HttpFetcher, RobotsChecker, UrlSeeder, extract_to_markdown
from libref.services.search import SearchService
from libref.store.document_store import DocumentStore

_DEFAULT_SEED_HOST_TTL = 7.0


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
        return asyncio.run(self._scrape_async(library, version, start_url,
                                              max_pages=max_pages, prefilter=prefilter))

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
        return counts

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
