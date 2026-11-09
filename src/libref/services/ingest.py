"""抓取编排：发现地址、抓取、净化、分块、落库、同步向量。

编排层是唯一的调度位置，出口层只调本模块。向量是派生索引：
真源落库成功后才同步向量，向量失败只计数，不推翻真源写入。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

from libref.chunking import ChunkPath, ensure_hard_limit, split_markdown_dual
from libref.chunking.budget import ChunkBudget, budgets_from_settings
from libref.crawler import (
    HttpFetcher,
    RobotsChecker,
    UrlSeeder,
    build_scope,
    extract_to_markdown,
    looks_like_js_rendered,
)
from libref.llm import EmbedFn
from libref.services.jobs import Job, JobManager
from libref.services.libraries import LibraryService
from libref.store.document_store import DocumentInput, DocumentStore, StoreResult
from libref.store.vec_store import QdrantVecStore
from libref.text.embedding import build_embed_text, is_embeddable_metadata
from libref.text.normalize import (
    classify_content,
    normalize_markdown,
    wrap_source_content,
)

logger = logging.getLogger(__name__)

SCRAPE_JOB_KIND = "scrape_docs"
"""抓取任务在任务表中的类型名。"""


def scrape_job_params(
    library: str,
    version: str,
    start_url: str,
    *,
    max_pages: int | None = None,
    prefilter: bool = False,
    scope: dict[str, Any] | None = None,
    ecosystem: str = "custom",
    resolved_from: str | None = None,
    registry_confirmed: bool = True,
) -> dict[str, Any]:
    """构造抓取任务参数，含解析来源以保证可重放。"""
    return {
        "library": library,
        "version": version,
        "start_url": start_url,
        "max_pages": max_pages,
        "prefilter": prefilter,
        "scope": scope or {},
        "ecosystem": ecosystem,
        "resolved_from": resolved_from,
        "registry_confirmed": registry_confirmed,
    }


def start_scrape_job(
    mgr: JobManager,
    library: str,
    version: str,
    start_url: str,
    *,
    max_pages: int | None = None,
    prefilter: bool = False,
    scope: dict[str, Any] | None = None,
    ecosystem: str = "custom",
    resolved_from: str | None = None,
    registry_confirmed: bool = True,
) -> int:
    """提交抓取任务到队列，返回任务标识，执行由 worker 加 runner 完成。"""
    return mgr.submit(
        SCRAPE_JOB_KIND,
        scrape_job_params(
            library,
            version,
            start_url,
            max_pages=max_pages,
            prefilter=prefilter,
            scope=scope,
            ecosystem=ecosystem,
            resolved_from=resolved_from,
            registry_confirmed=registry_confirmed,
        ),
    )


def start_refresh_job(
    mgr: JobManager,
    library: str,
    version: str,
    start_url: str,
    *,
    max_pages: int | None = None,
    prefilter: bool = False,
    scope: dict[str, Any] | None = None,
    ecosystem: str = "custom",
) -> int:
    """提交刷新任务到队列，复用抓取任务类型执行增量语义。"""
    return mgr.submit(
        SCRAPE_JOB_KIND,
        scrape_job_params(
            library,
            version,
            start_url,
            max_pages=max_pages,
            prefilter=prefilter,
            scope=scope,
            ecosystem=ecosystem,
            resolved_from="refresh",
            registry_confirmed=True,
        ),
    )


_version_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
_version_locks_guard = threading.Lock()


def _version_lock(ecosystem: str, library: str, version: str) -> asyncio.Lock:
    """同版本同时只允许一个抓取任务执行，第二个任务排队等待。"""
    key = (ecosystem, library, version)
    with _version_locks_guard:
        lock = _version_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _version_locks[key] = lock
        return lock


def build_scrape_runner(settings, conn, *, vec_store=None, embed=None, mgr=None):
    """构造任务消费函数，按任务参数执行整站抓取。"""
    service = IngestService(settings, conn, vec_store=vec_store, embed=embed)

    async def runner(job: Job):
        if job.kind != SCRAPE_JOB_KIND:
            raise ValueError(f"unsupported job kind: {job.kind}")
        params = job.params
        heartbeat = (lambda: mgr.heartbeat(job.id)) if mgr is not None else None
        return await service.scrape_docs_async(
            params["library"],
            params["version"],
            params["start_url"],
            max_pages=params.get("max_pages"),
            prefilter=params.get("prefilter", False),
            scope=params.get("scope"),
            ecosystem=params.get("ecosystem", "custom"),
            resolved_from=params.get("resolved_from"),
            registry_confirmed=params.get("registry_confirmed", True),
            heartbeat=heartbeat,
        )

    return runner


def run_scrape_job_sync(
    mgr: JobManager,
    settings,
    conn,
    job_id: int,
    *,
    timeout: float = 300.0,
    vec_store=None,
    embed: EmbedFn | None = None,
) -> Job:
    """同步驱动单任务：起 worker、等待落定、停 worker，供命令行异步开关使用。"""

    async def _drive() -> Job:
        mgr.start_worker(
            build_scrape_runner(settings, conn, vec_store=vec_store, embed=embed, mgr=mgr)
        )
        try:
            return await mgr.wait_for(job_id, timeout=timeout)
        finally:
            mgr.stop_worker()

    return asyncio.run(_drive())


@asynccontextmanager
async def _client(settings) -> AsyncIterator[httpx.AsyncClient]:
    from libref.crawler.http import build_async_client

    async with build_async_client(settings) as client:
        yield client


def _doc_budgets(settings) -> dict[ChunkPath, ChunkBudget]:
    """按配置生成文档分块的双路预算，分块预算的唯一装配点。"""
    return budgets_from_settings(settings)


PIPELINE_STEPS = ("discover", "fetch", "clean", "split", "store", "vectors")
"""抓取阶段顺序，耗时计数键按该顺序取各阶段名加毫秒后缀。"""


def _budget_fingerprint(budgets: dict[ChunkPath, ChunkBudget]) -> str:
    """分块预算指纹，预算变更后净化缓存旧条目不再命中。"""
    parts = []
    for path in sorted(budgets, key=lambda p: p.value):
        budget = budgets[path]
        parts.append(
            f"{path.value}:{budget.body_limit}:{budget.min_limit}:{budget.overlap}"
        )
    return "|".join(parts)


def normalize_text_content(raw: str) -> str:
    """Plain text and Markdown normalization without HTML parsing."""
    return normalize_markdown(raw)


def _plan_targets(seed, start_url: str, scope: dict[str, Any] | None, max_pages: int | None):
    """抓取目标规划：作用域过滤加起始页回退加分页截断，纯决策不碰库。"""
    policy = build_scope(start_url, scope)
    candidates = seed.urls + [u for u in seed.cross_site if u not in seed.urls]
    accepted, rejected = policy.filter(candidates)
    if not accepted and policy.accept(start_url):
        accepted = [start_url]
    ingest_truncated = False
    if max_pages is not None and len(accepted) > max_pages:
        accepted = accepted[:max_pages]
        ingest_truncated = True
    truncated = bool(seed.truncated or ingest_truncated)
    return accepted, rejected, truncated


def _record_outcome(counts, removed_all, inserted_all, result, empty_dropped, render_skipped, stage_ms) -> None:
    """单页结局汇总：计数、向量候选、阶段耗时，一处归口。"""
    counts[result.outcome] = counts.get(result.outcome, 0) + 1
    counts["chunk_empty_dropped"] += empty_dropped
    if render_skipped:
        counts["render_skipped"] += 1
    counts["chunk_reused"] += len(getattr(result, "reused_ids", []))
    counts["snapshot_written"] += int(stage_ms.get("snapshot_written", 0))
    counts["snapshot_quota_skipped"] += int(stage_ms.get("snapshot_quota_skipped", 0))
    for step in PIPELINE_STEPS:
        counts[f"{step}_ms"] += int(stage_ms.get(f"{step}_ms", 0))
    removed_all.extend(result.removed_ids)
    inserted_all.extend(result.inserted_ids)


def _build_doc_inputs(raw_chunks, content_type: str | None, kind: str) -> list[DocumentInput]:
    """文档块输入组装：排序号与类型回填，落库前唯一成形点。"""
    for order, chunk in enumerate(raw_chunks):
        chunk.sort_order = order
        if not getattr(chunk, "content_type", "") or chunk.content_type == "text/markdown":
            chunk.content_type = (content_type or "text/markdown").split(";")[0].strip() or kind
    return [
        DocumentInput(
            url=c.url,
            path=c.path,
            title=c.title,
            content=c.content,
            sort_order=c.sort_order,
            chunk_path=getattr(c, "chunk_path", "embedding"),
            source_group_id=getattr(c, "source_group_id", ""),
            chunk_id=getattr(c, "chunk_id", ""),
            span_kind=getattr(c, "span_kind", "document_range"),
            merged_from=list(getattr(c, "merged_from", []) or []),
            overlap_from=getattr(c, "overlap_from", None),
            content_hash=getattr(c, "content_hash", ""),
            keywords=list(getattr(c, "keywords", []) or []),
            content_type=getattr(c, "content_type", "text/markdown"),
            start_char=getattr(c, "start_char", -1),
            end_char=getattr(c, "end_char", -1),
            prev_chunk_id=getattr(c, "prev_chunk_id", None),
            next_chunk_id=getattr(c, "next_chunk_id", None),
        )
        for c in raw_chunks
    ]


class IngestService:
    def __init__(
        self,
        settings,
        conn,
        *,
        vec_store: QdrantVecStore | None = None,
        embed: EmbedFn | None = None,
    ) -> None:
        self._settings = settings
        self._conn = conn
        self._vec = vec_store if embed is not None else None
        self._embed = embed if vec_store is not None else None
        self._budgets = _doc_budgets(settings)
        self._clean_fingerprint = _budget_fingerprint(self._budgets)
        self._clean_cache: dict[str, str] = {}
        self._snapshots = None
        if bool(settings.snapshot_enabled):
            from libref.store.snapshots import SnapshotStore
            self._snapshots = SnapshotStore(
                Path(settings.snapshot_dir),
                quota_mb=int(settings.snapshot_quota_mb),
            )

    def scrape_docs(
        self,
        library: str,
        version: str,
        start_url: str,
        *,
        max_pages: int | None = None,
        prefilter: bool = False,
        scope: dict[str, Any] | None = None,
        ecosystem: str = "custom",
        resolved_from: str | None = None,
        registry_confirmed: bool = True,
        homepage: str | None = None,
        repo_url: str | None = None,
        heartbeat=None,
    ) -> dict[str, int]:
        """同步入口：发现、抓取、净化、分块、落库，返回各类结局计数。"""
        return asyncio.run(
            self.scrape_docs_async(
                library,
                version,
                start_url,
                max_pages=max_pages,
                prefilter=prefilter,
                scope=scope,
                ecosystem=ecosystem,
                resolved_from=resolved_from,
                registry_confirmed=registry_confirmed,
                homepage=homepage,
                repo_url=repo_url,
                heartbeat=heartbeat,
            )
        )

    async def scrape_docs_async(
        self,
        library: str,
        version: str,
        start_url: str,
        *,
        max_pages: int | None = None,
        prefilter: bool = False,
        scope: dict[str, Any] | None = None,
        ecosystem: str = "custom",
        resolved_from: str | None = None,
        registry_confirmed: bool = True,
        homepage: str | None = None,
        repo_url: str | None = None,
        heartbeat=None,
    ) -> dict[str, int]:
        """异步入口，任务 worker 复用，避免在运行中的事件循环内嵌套启动。"""
        return await self._scrape_async(
            library,
            version,
            start_url,
            max_pages=max_pages,
            prefilter=prefilter,
            scope=scope,
            ecosystem=ecosystem,
            resolved_from=resolved_from,
            registry_confirmed=registry_confirmed,
            homepage=homepage,
            repo_url=repo_url,
            heartbeat=heartbeat,
        )

    def refresh_version(
        self,
        library: str,
        version: str,
        start_url: str,
        *,
        max_pages: int | None = None,
        prefilter: bool = False,
        scope: dict[str, Any] | None = None,
        ecosystem: str = "custom",
        heartbeat=None,
    ) -> dict[str, int]:
        """刷新意图入口：复用抓取链路的增量语义，只表达刷新意图。"""
        return self.scrape_docs(
            library, version, start_url, max_pages=max_pages, prefilter=prefilter,
            scope=scope, ecosystem=ecosystem, resolved_from="refresh",
            registry_confirmed=True, heartbeat=heartbeat,
        )

    async def refresh_version_async(
        self,
        library: str,
        version: str,
        start_url: str,
        *,
        max_pages: int | None = None,
        prefilter: bool = False,
        scope: dict[str, Any] | None = None,
        ecosystem: str = "custom",
        heartbeat=None,
    ) -> dict[str, int]:
        """刷新异步入口，供任务复用。"""
        return await self.scrape_docs_async(
            library, version, start_url, max_pages=max_pages, prefilter=prefilter,
            scope=scope, ecosystem=ecosystem, resolved_from="refresh",
            registry_confirmed=True, heartbeat=heartbeat,
        )

    def scrape_by_name(
        self,
        ecosystem: str | None,
        package: str,
        requested_version: str | None = None,
        *,
        max_pages: int | None = None,
        prefilter: bool = False,
        scope: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        """按名称解析后抓取：先解析出地址与版本，再走标准抓取链路。"""
        return asyncio.run(
            self.scrape_by_name_async(
                ecosystem,
                package,
                requested_version,
                max_pages=max_pages,
                prefilter=prefilter,
                scope=scope,
            )
        )

    async def scrape_by_name_async(
        self,
        ecosystem: str | None,
        package: str,
        requested_version: str | None = None,
        *,
        max_pages: int | None = None,
        prefilter: bool = False,
        scope: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        """按名称解析后抓取的异步入口，供任务 worker 复用。"""
        from libref.services.resolver import resolve_package

        async with _client(self._settings) as client:
            resolution = await resolve_package(
                self._conn,
                client,
                self._settings,
                ecosystem,
                package,
                requested_version,
            )
        assert resolution.docs_url is not None
        return await self._scrape_async(
            resolution.name,
            resolution.version,
            resolution.docs_url,
            max_pages=max_pages,
            prefilter=prefilter,
            scope=scope,
            ecosystem=resolution.ecosystem,
            resolved_from=resolution.source,
            registry_confirmed=resolution.registry_confirmed,
            homepage=resolution.homepage,
            repo_url=resolution.repo_url,
        )

    async def _scrape_async(
        self,
        library: str,
        version: str,
        start_url: str,
        *,
        max_pages: int | None,
        prefilter: bool,
        scope: dict[str, Any] | None = None,
        ecosystem: str = "custom",
        resolved_from: str | None = None,
        registry_confirmed: bool = True,
        homepage: str | None = None,
        repo_url: str | None = None,
        heartbeat=None,
    ) -> dict[str, int]:
        import time as _time
        started = _time.monotonic()

        def _beat() -> None:
            if heartbeat is None:
                return
            try:
                maybe = heartbeat()
                if asyncio.iscoroutine(maybe):
                    asyncio.get_running_loop().create_task(maybe)
            except Exception:  # noqa: BLE001 —— 心跳只求续写
                pass

        counts = {
            k: 0
            for k in (
                "stored",
                "unchanged",
                "absent",
                "empty",
                "skipped",
                "failed",
                "vector_failed",
                "chunk_empty_dropped",
                "chunk_truncated",
                "render_skipped",
                "chunk_reused",
                "seed_discovered",
                "scope_rejected",
                "seed_truncated",
                "snapshot_written",
                "snapshot_quota_skipped",
                "elapsed_ms",
                *(f"{step}_ms" for step in PIPELINE_STEPS),
            )
        }
        async with _version_lock(ecosystem, library, version):
            removed_all: list[int] = []
            inserted_all: list[int] = []
            async with _client(self._settings) as client:
                robots = RobotsChecker(
                    self._conn,
                    client,
                    ttl_days=self._settings.robots_cache_ttl_days,
                    user_agent=self._settings.user_agent,
                )
                seeder = UrlSeeder(client, robots)
                fetcher = HttpFetcher(
                    client,
                    max_retries=self._settings.fetch_max_retries,
                    default_delay=self._settings.crawl_delay_default,
                )
                discover_start = _time.monotonic()
                seed = await seeder.discover(
                    start_url, prefilter=prefilter, max_pages=None
                )
                counts["discover_ms"] = int((_time.monotonic() - discover_start) * 1000)
                accepted, rejected, truncated = _plan_targets(seed, start_url, scope, max_pages)
                counts["skipped"] += len(rejected)
                counts["scope_rejected"] += len(rejected)
                counts["seed_discovered"] += len(seed.urls)
                if seed.source == "none" and seed.failed:
                    counts["failed"] += 1

                store = DocumentStore(self._conn)
                catalog = LibraryService(self._conn)
                library_id, version_id = catalog.ensure_library_version(
                    library,
                    version,
                    ecosystem,
                    homepage=homepage,
                    docs_url=start_url,
                    repo_url=repo_url,
                    resolved_from=resolved_from
                    or ("explicit" if ecosystem == "custom" else None),
                    registry_confirmed=registry_confirmed,
                )

                sem = asyncio.Semaphore(self._settings.fetch_concurrency)

                async def process(url: str) -> None:
                    async with sem:
                        try:
                            page = store.get_page(version_id, url)
                            result, empty_dropped, render_skipped, stage_ms = await self._fetch_and_store(
                                fetcher,
                                robots,
                                store,
                                library_id,
                                version_id,
                                url,
                                page,
                                scope=scope,
                                snapshot_ctx=(ecosystem, library, version),
                            )
                        except Exception as exc:  # noqa: BLE001 —— 单页失败记结局，不阻断整批
                            logger.debug("page failed url=%s: %s", url, exc)
                            result, empty_dropped, render_skipped, stage_ms = StoreResult("failed"), 0, False, {}
                        _record_outcome(counts, removed_all, inserted_all, result, empty_dropped, render_skipped, stage_ms)
                        _beat()

                await asyncio.gather(*(process(u) for u in accepted))
                removed_all.extend(
                    self._sweep_absent(
                        store,
                        library_id,
                        version_id,
                        seed,
                        accepted,
                        truncated=truncated,
                        counts=counts,
                        snapshot_ctx=(ecosystem, library, version),
                    )
                )
                vectors_start = _time.monotonic()
                await self._sync_vectors(store, counts, removed_all, inserted_all)
                counts["vectors_ms"] = int((_time.monotonic() - vectors_start) * 1000)
                _beat()
            import time as _time2
            counts["seed_truncated"] = 1 if truncated else 0
            counts["elapsed_ms"] = int((_time2.monotonic() - started) * 1000)
            logger.info(
                "scrape done %s:%s %s stored=%d unchanged=%d absent=%d failed=%d skipped=%d",
                ecosystem, library, version, counts.get("stored", 0),
                counts.get("unchanged", 0), counts.get("absent", 0),
                counts.get("failed", 0), counts.get("skipped", 0),
            )
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
                self._settings.embedding_model,
                self._settings.embedding_dim,
            )
        except Exception as exc:  # noqa: BLE001
            counts["vector_failed"] += len(removed) + len(inserted)
            logger.warning("vector ensure collection failed: %s", exc)
            return
        try:
            self._vec.delete_docs(removed)
        except Exception as exc:  # noqa: BLE001
            counts["vector_failed"] += len(removed) + len(inserted)
            logger.warning("vector delete failed: %s", exc)
            return
        try:
            docs = [
                d for d in store.get_documents(inserted)
                if is_embeddable_metadata(d["metadata"])
            ]
            hard = int(self._settings.chunk_embed_max_tokens)
            texts = []
            for d in docs:
                meta = d["metadata"] or {}
                text = build_embed_text(
                    meta.get("title"), meta.get("path"), d["content"],
                )
                clipped, was_clipped = ensure_hard_limit(text, ChunkPath.EMBEDDING, hard)
                if was_clipped:
                    counts["chunk_truncated"] = counts.get("chunk_truncated", 0) + 1
                    logger.warning(
                        "embedding text clipped to %d tokens for doc %d", hard, d["id"]
                    )
                texts.append(clipped)
            vecs = await self._embed(texts) if texts else []
            if len(vecs) != len(docs):
                raise RuntimeError("embedding count mismatch")
            self._vec.upsert_batch(
                [
                    (d["id"], d["library_id"], d["version_id"], vec)
                    for d, vec in zip(docs, vecs)
                ]
            )
        except Exception as exc:  # noqa: BLE001
            counts["vector_failed"] += len(inserted)
            logger.warning("vector upsert failed: %s", exc)

    def _sweep_absent(
        self,
        store: DocumentStore,
        library_id: int,
        version_id: int,
        seed,
        accepted_urls: list[str],
        *,
        truncated: bool,
        counts: dict[str, int],
        snapshot_ctx: tuple[str, str, str] | None = None,
    ) -> list[int]:
        if seed.source == "none":
            return []
        if seed.failed or truncated:
            return []
        removed: list[int] = []
        removed_urls: list[str] = []
        discovered = set(accepted_urls)
        for url in store.list_page_urls(version_id):
            if not url.startswith(("http://", "https://")):
                continue
            if url not in discovered:
                result = store.store_page(version_id, library_id, url, "absent")
                removed.extend(result.removed_ids)
                removed_urls.append(url)
                counts["absent"] = counts.get("absent", 0) + 1
        if removed_urls and snapshot_ctx is not None:
            try:
                from libref.services.retention import REASON_ABSENT_SWEEP, log_deletion
                ecosystem, library, version = snapshot_ctx
                log_deletion(
                    self._conn,
                    library=library,
                    version=version,
                    ecosystem=ecosystem,
                    reason=REASON_ABSENT_SWEEP,
                    detail={
                        "urls": removed_urls[:20],
                        "url_count": len(removed_urls),
                        "removed_docs": len(removed),
                    },
                )
                logger.info("absent sweep %s:%s %s urls=%d docs=%d",
                            ecosystem, library, version, len(removed_urls), len(removed))
            except Exception as exc:  # noqa: BLE001 —— 留痕失败不推翻真源删除
                logger.debug("absent log failed: %s", exc)
        return removed

    def _clean_with_cache(self, kind: str, raw: str, url: str) -> str:
        """净化缓存：同内容与同预算指纹命中直接复用，超限逐出最早条目。"""
        import hashlib as _hashlib
        digest = _hashlib.sha1(
            f"{kind}\0{self._clean_fingerprint}\0{raw}".encode("utf-8", "ignore")
        ).hexdigest()
        hit = self._clean_cache.get(digest)
        if hit is not None:
            return hit
        if kind == "markdown" or kind == "text":
            cleaned = normalize_text_content(raw)
        elif kind == "source":
            cleaned = wrap_source_content(raw, url)
        else:
            try:
                cleaned = extract_to_markdown(raw, url)
            except Exception:  # noqa: BLE001
                cleaned = ""
        while len(self._clean_cache) >= self._settings.ingest_clean_cache_cap:
            oldest = next(iter(self._clean_cache))
            del self._clean_cache[oldest]
        self._clean_cache[digest] = cleaned
        return cleaned

    async def _fetch_and_store(
        self,
        fetcher: HttpFetcher,
        robots: RobotsChecker,
        store: DocumentStore,
        library_id: int,
        version_id: int,
        url: str,
        page,
        *,
        scope: dict[str, Any] | None = None,
        snapshot_ctx: tuple[str, str, str] | None = None,
    ) -> tuple[StoreResult, int, bool, dict[str, int]]:
        import time as _time
        stage_ms: dict[str, int] = {f"{step}_ms": 0 for step in PIPELINE_STEPS}
        stage_ms["snapshot_written"] = 0
        stage_ms["snapshot_quota_skipped"] = 0
        fetch_start = _time.monotonic()
        if not await robots.can_fetch(url):
            return StoreResult("skipped"), 0, False, stage_ms
        delay = await robots.crawl_delay(url)
        result = await fetcher.fetch(
            url,
            etag=page.etag if page else None,
            last_modified=page.last_modified if page else None,
            crawl_delay=delay,
        )
        if result.status == "unchanged":
            stage_ms["fetch_ms"] = int((_time.monotonic() - fetch_start) * 1000)
            return store.store_page(version_id, library_id, url, "unchanged"), 0, False, stage_ms
        if result.status == "absent":
            stage_ms["fetch_ms"] = int((_time.monotonic() - fetch_start) * 1000)
            return store.store_page(version_id, library_id, url, "absent"), 0, False, stage_ms
        if result.status == "failed":
            stage_ms["fetch_ms"] = int((_time.monotonic() - fetch_start) * 1000)
            return store.store_page(version_id, library_id, url, "failed"), 0, False, stage_ms
        if self._snapshots is not None and snapshot_ctx is not None and result.content:
            try:
                ecosystem, library, version = snapshot_ctx
                _, written = self._snapshots.put(
                    ecosystem, library, version, url,
                    result.content.encode("utf-8", errors="ignore"),
                    result.content_type,
                )
                if written:
                    stage_ms["snapshot_written"] = 1
                else:
                    stage_ms["snapshot_quota_skipped"] = 1
            except Exception:  # noqa: BLE001
                pass

        raw_body = result.content or ""
        kind = classify_content(result.content_type, url)
        clean_start = _time.monotonic()
        markdown = self._clean_with_cache(kind, raw_body, url)
        render_attempted = False
        if not markdown:
            if looks_like_js_rendered(raw_body, markdown):
                render_attempted = True
                want_render = bool((scope or {}).get("render_js", False) or self._settings.render_js_default)
                if want_render:
                    try:
                        from libref.crawler.browser import (
                            is_playwright_available,
                            render_html,
                        )
                        rendered = await render_html(url) if is_playwright_available() else None
                    except Exception:  # noqa: BLE001
                        rendered = None
                    if rendered:
                        markdown = self._clean_with_cache("html", rendered, url)
                if not markdown:
                    stage_ms["fetch_ms"] = int((clean_start - fetch_start) * 1000)
                    stage_ms["clean_ms"] = int((_time.monotonic() - clean_start) * 1000)
                    return StoreResult("skipped"), 0, render_attempted, stage_ms
            stage_ms["fetch_ms"] = int((clean_start - fetch_start) * 1000)
            stage_ms["clean_ms"] = int((_time.monotonic() - clean_start) * 1000)
            return store.store_page(
                version_id,
                library_id,
                url,
                "empty",
                raw_content=result.content,
                etag=result.etag,
                last_modified=result.last_modified,
            ), 0, render_attempted, stage_ms
        stage_ms["fetch_ms"] = int((clean_start - fetch_start) * 1000)
        clean_end = _time.monotonic()
        stage_ms["clean_ms"] = int((clean_end - clean_start) * 1000)
        store_start = clean_end
        outcome = store.ensure_changed(version_id, url, markdown)
        if outcome == "unchanged":
            stage_ms["store_ms"] = int((_time.monotonic() - store_start) * 1000)
            return store.store_page(version_id, library_id, url, "unchanged"), 0, render_attempted, stage_ms
        split_start = _time.monotonic()
        dual = split_markdown_dual(markdown, url, budgets=self._budgets)
        split_end = _time.monotonic()
        stage_ms["split_ms"] = int((split_end - split_start) * 1000)
        raw_chunks = dual.embedding + dual.bm25
        inputs = _build_doc_inputs(raw_chunks, result.content_type, kind)
        stored = store.store_page(
            version_id,
            library_id,
            url,
            "stored",
            chunks=inputs,
            raw_content=markdown,
            etag=result.etag,
            last_modified=result.last_modified,
        )
        stage_ms["store_ms"] = int((_time.monotonic() - split_end) * 1000)
        return stored, dual.empty_dropped, render_attempted, stage_ms


