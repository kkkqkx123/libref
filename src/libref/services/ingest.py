"""抓取编排：发现地址、抓取、净化、分块、落库、同步向量。

编排层是唯一的调度位置，出口层只调本模块。向量是派生索引：
真源落库成功后才同步向量，向量失败只计数，不推翻真源写入。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx

from libref.chunking import split_markdown
from libref.crawler import HttpFetcher, RobotsChecker, UrlSeeder, extract_to_markdown
from libref.services.jobs import Job, JobManager
from libref.services.search import SearchService
from libref.store.document_store import DocumentStore, StoreResult
from libref.store.vec_store import QdrantVecStore, build_embed_text

_DEFAULT_SEED_HOST_TTL = 7.0
_EMBED_BATCH = 64

SCRAPE_JOB_KIND = "scrape_docs"
"""抓取任务在任务表中的类型名。"""

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]
"""批量编码函数：文本列表进，向量列表出，顺序对应。"""


def scrape_job_params(
    library: str,
    version: str,
    start_url: str,
    *,
    max_pages: int | None = None,
    prefilter: bool = False,
) -> dict[str, Any]:
    """构造抓取任务参数。"""
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
    """提交抓取任务到队列，返回任务标识，执行由 worker 加 runner 完成。"""
    return mgr.submit(
        SCRAPE_JOB_KIND,
        scrape_job_params(library, version, start_url,
                          max_pages=max_pages, prefilter=prefilter),
    )


def build_embed_fn(settings) -> EmbedFn | None:
    """按配置构造编码函数，无密钥时返回空，调用方走纯全文路。"""
    api_key = getattr(settings, "embedding_api_key", None)
    if not api_key:
        return None
    base = getattr(settings, "embedding_api_base", "")
    model = getattr(settings, "embedding_model", "")
    dim = getattr(settings, "embedding_dim", 0)
    timeout = getattr(settings, "request_timeout", 30.0)

    async def embed(texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=timeout) as client:
            for i in range(0, len(texts), _EMBED_BATCH):
                out.extend(
                    await _embed_batch(client, base, api_key, model, dim,
                                       texts[i:i + _EMBED_BATCH])
                )
        return out

    return embed


async def _embed_batch(
    client: httpx.AsyncClient,
    base: str,
    key: str,
    model: str,
    dim: int,
    texts: list[str],
    *,
    retries: int = 3,
) -> list[list[float]]:
    """调兼容接口批量编码，429 与服务端错误退避重试，其余失败直接抛错。"""
    url = base.rstrip("/") + "/embeddings"
    headers = {"Authorization": f"Bearer {key}"}
    body = {"model": model, "input": texts}
    error: str | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(0.5 * (2**attempt))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            error = f"HTTP {resp.status_code}"
            await asyncio.sleep(0.5 * (2**attempt))
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"embedding rejected: HTTP {resp.status_code}")
        items = sorted(resp.json()["data"], key=lambda d: d["index"])
        vecs: list[list[float]] = []
        for item in items:
            vec = item["embedding"]
            norm = sum(x * x for x in vec) ** 0.5
            vec = [x / norm for x in vec] if norm > 0 else vec
            if dim and len(vec) != dim:
                raise RuntimeError(f"embedding dim mismatch: got {len(vec)}, want {dim}")
            vecs.append(vec)
        if len(vecs) != len(texts):
            raise RuntimeError("embedding count mismatch")
        return vecs
    raise RuntimeError(f"embedding failed: {error}")


def build_scrape_runner(settings, conn, *, vec_store=None, embed=None):
    """构造任务消费函数，按任务参数执行整站抓取。"""
    service = IngestService(settings, conn, vec_store=vec_store, embed=embed)

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
                        *, timeout: float = 300.0,
                        vec_store=None, embed: EmbedFn | None = None) -> Job:
    """同步驱动单任务：起 worker、等待落定、停 worker，供命令行异步开关使用。"""
    async def _drive() -> Job:
        mgr.start_worker(build_scrape_runner(settings, conn,
                                             vec_store=vec_store, embed=embed))
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
    def __init__(self, settings, conn, *, vec_store: QdrantVecStore | None = None,
                 embed: EmbedFn | None = None) -> None:
        self._settings = settings
        self._conn = conn
        self._vec = vec_store if embed is not None else None
        self._embed = embed if vec_store is not None else None

    def scrape_docs(
        self,
        library: str,
        version: str,
        start_url: str,
        *,
        max_pages: int | None = None,
        prefilter: bool = False,
    ) -> dict[str, int]:
        """同步入口：发现、抓取、净化、分块、落库，返回各类结局计数。"""
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
        """异步入口，任务 worker 复用，避免在运行中的事件循环内嵌套启动。"""
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
                  ("stored", "unchanged", "absent", "empty", "skipped", "failed",
                   "vector_failed")}
        removed_all: list[int] = []
        inserted_all: list[int] = []
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
                    result = await self._fetch_and_store(
                        fetcher, robots, store, library_id, version_id, url, page
                    )
                    counts[result.outcome] = counts.get(result.outcome, 0) + 1
                    removed_all.extend(result.removed_ids)
                    inserted_all.extend(result.inserted_ids)

            await asyncio.gather(*(process(u) for u in seed.urls))
            removed_all.extend(
                self._sweep_absent(store, library_id, version_id, seed,
                                   max_pages=max_pages, counts=counts)
            )
            await self._sync_vectors(store, counts, removed_all, inserted_all)
        return counts

    async def _sync_vectors(
        self,
        store: DocumentStore,
        counts: dict[str, int],
        removed: list[int],
        inserted: list[int],
    ) -> None:
        """收尾批量同步向量：集合校验、删旧点、编码、写入，分段计数失败。"""
        if self._vec is None or self._embed is None:
            return
        if not removed and not inserted:
            return
        assert self._vec is not None and self._embed is not None
        try:
            self._vec.ensure_collection(
                self._conn,
                getattr(self._settings, "embedding_model", ""),
                getattr(self._settings, "embedding_dim", 0),
            )
        except Exception:  # noqa: BLE001 —— 派生索引失败只计数，不推翻真源写入
            counts["vector_failed"] += len(removed) + len(inserted)
            return
        try:
            self._vec.delete_docs(removed)
        except Exception:  # noqa: BLE001 —— 派生索引失败只计数，不推翻真源写入
            counts["vector_failed"] += len(removed) + len(inserted)
            return
        try:
            docs = store.get_documents(inserted)
            texts = [
                build_embed_text(d["metadata"].get("title"), d["metadata"].get("path"),
                                 d["content"])
                for d in docs
            ]
            vecs = await self._embed(texts) if texts else []
            if len(vecs) != len(docs):
                raise RuntimeError("embedding count mismatch")
            self._vec.upsert_batch([
                (d["id"], d["library_id"], d["version_id"], vec)
                for d, vec in zip(docs, vecs)
            ])
        except Exception:  # noqa: BLE001 —— 派生索引失败只计数，不推翻真源写入
            counts["vector_failed"] += len(inserted)

    def _sweep_absent(self, store: DocumentStore, library_id: int, version_id: int,
                      seed, *, max_pages: int | None,
                      counts: dict[str, int]) -> list[int]:
        """缺席清理：已有页面减去本次发现集，返回删掉的文档标识。

        发现截断或发现源为空时跳过，避免把未覆盖或瞬时失败的页面误删。
        """
        if seed.source == "none":
            return []
        if max_pages is not None and len(seed.urls) >= max_pages:
            return []
        removed: list[int] = []
        discovered = set(seed.urls)
        for url in store.list_page_urls(version_id):
            if url not in discovered:
                result = store.store_page(version_id, library_id, url, "absent")
                removed.extend(result.removed_ids)
                counts["absent"] = counts.get("absent", 0) + 1
        return removed

    async def _fetch_and_store(
        self,
        fetcher: HttpFetcher,
        robots: RobotsChecker,
        store: DocumentStore,
        library_id: int,
        version_id: int,
        url: str,
        page,
    ) -> StoreResult:
        if not await robots.can_fetch(url):
            return StoreResult("skipped")
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
