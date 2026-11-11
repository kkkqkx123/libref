"""Scrape orchestration: discover URLs, fetch, clean, chunk, store, sync vectors.

The orchestration layer is the only scheduling location; exit layers only call this module. Vectors are a derived index:
vectors are synced only after the source-of-truth store succeeds; vector failures are only counted and never overturn the source write.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from libref.chunking import ChunkPath, split_markdown_dual
from libref.chunking.budget import ChunkBudget, budgets_from_settings
from libref.crawler import (
    HttpFetcher,
    RobotsChecker,
    UrlSeeder,
    build_scope,
    looks_like_js_rendered,
    scrape_html,
)
from libref.crawler.extract import ScrapeResult
from libref.crawler.scope import sort_by_relevance
from libref.llm import EmbedFn
from libref.services.infra.jobs import Job, JobManager
from libref.services.infra.version_lock import version_lock as _version_lock
from libref.services.registry.libraries import LibraryService
from libref.store.document_store import DocumentInput, DocumentStore, StoreResult
from libref.store.vec_store import VectorStore
from libref.text.normalize import (
    classify_content,
    normalize_markdown,
    wrap_source_content,
)

logger = logging.getLogger(__name__)

SCRAPE_JOB_KIND = "scrape_docs"
"""Job kind name for scrape jobs in the job table."""


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
    """Build scrape job params, including the resolution source for replayability."""
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
    """Submit a scrape job to the queue and return its id; execution is done by the worker plus runner."""
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
    """Submit a refresh job to the queue, reusing the scrape job kind for incremental semantics."""
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


def build_scrape_runner(settings, conn, *, vec_store=None, embed=None, mgr=None):
    """Build the job consumer function that runs a full-site scrape per job params."""
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
    """Drive a single job synchronously: start worker, wait for settle, stop worker; for the CLI's async toggle."""

    async def _drive() -> Job:
        mgr.start_worker(
            build_scrape_runner(
                settings, conn, vec_store=vec_store, embed=embed, mgr=mgr
            )
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
    """Build the two-path document chunking budgets from settings; the single assembly point for chunk budgets."""
    return budgets_from_settings(settings)


PIPELINE_STEPS = ("discover", "fetch", "clean", "split", "store", "vectors")
"""Scrape stage order; timing count keys take each stage name plus a millisecond suffix in this order."""


def _budget_fingerprint(budgets: dict[ChunkPath, ChunkBudget]) -> str:
    """Chunk budget fingerprint; after a budget change old cleaning-cache entries no longer hit."""
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


@dataclass
class _FetchDetail:
    """Per-page follow-up data: followable out-links plus the canonical alias."""

    links: list[str] = field(default_factory=list)
    canonical: str = ""


def _plan_targets(
    seed, start_url: str, scope: dict[str, Any] | None, max_pages: int | None
):
    """Scrape target planning: scope filter plus start-page fallback plus pagination truncation; pure decisions, no DB access."""
    policy = build_scope(start_url, scope)
    candidates = seed.urls + [u for u in seed.cross_site if u not in seed.urls]
    accepted, rejected, stats = policy.filter_with_stats(candidates)
    if not accepted and policy.accept(start_url):
        accepted = [start_url]
    query = (scope or {}).get("crawl_query")
    if query and query.strip():
        accepted = sort_by_relevance(accepted, query)
    ingest_truncated = False
    if max_pages is not None and len(accepted) > max_pages:
        accepted = accepted[:max_pages]
        ingest_truncated = True
    truncated = bool(seed.truncated or ingest_truncated)
    return accepted, rejected, truncated, stats


def _record_outcome(
    counts, removed_all, inserted_all, result, empty_dropped, render_skipped, stage_ms
) -> None:
    """Per-page outcome summary: counts, vector candidates, stage timings; one funnel."""
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


def _build_doc_inputs(
    raw_chunks, content_type: str | None, kind: str
) -> list[DocumentInput]:
    """Document chunk input assembly: sort order and type backfill; the single shaping point before storing."""
    for order, chunk in enumerate(raw_chunks):
        chunk.sort_order = order
        if (
            not getattr(chunk, "content_type", "")
            or chunk.content_type == "text/markdown"
        ):
            chunk.content_type = (content_type or "text/markdown").split(";")[
                0
            ].strip() or kind
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
            overlap_text=str(getattr(c, "overlap_text", "") or ""),
            chunk_index=int(getattr(c, "chunk_index", 0) or 0),
            total_chunks=max(1, int(getattr(c, "total_chunks", 1) or 1)),
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
        vec_store: VectorStore | None = None,
        embed: EmbedFn | None = None,
    ) -> None:
        from libref.store.vec_store import paired_vector

        self._settings = settings
        self._conn = conn
        self._vec, self._embed = paired_vector(vec_store, embed)
        self._budgets = _doc_budgets(settings)
        self._clean_fingerprint = _budget_fingerprint(self._budgets)
        self._clean_cache: dict[str, str] = {}
        self._scrape_cache: dict[str, ScrapeResult] = {}
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
        """Sync entry: discover, fetch, clean, chunk, store; returns outcome counts by kind."""
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
        """Async entry, reused by the job worker, to avoid nested loop startup inside a running event loop."""
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
        """Refresh-intent entry: reuses the scrape path's incremental semantics, only expressing refresh intent."""
        return self.scrape_docs(
            library,
            version,
            start_url,
            max_pages=max_pages,
            prefilter=prefilter,
            scope=scope,
            ecosystem=ecosystem,
            resolved_from="refresh",
            registry_confirmed=True,
            heartbeat=heartbeat,
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
        """Refresh async entry, for job reuse."""
        return await self.scrape_docs_async(
            library,
            version,
            start_url,
            max_pages=max_pages,
            prefilter=prefilter,
            scope=scope,
            ecosystem=ecosystem,
            resolved_from="refresh",
            registry_confirmed=True,
            heartbeat=heartbeat,
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
        include_prerelease: bool = False,
    ) -> dict[str, int]:
        """Scrape after resolving by name: resolve the address and version first, then run the standard scrape path."""
        return asyncio.run(
            self.scrape_by_name_async(
                ecosystem,
                package,
                requested_version,
                max_pages=max_pages,
                prefilter=prefilter,
                scope=scope,
                include_prerelease=include_prerelease,
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
        include_prerelease: bool = False,
    ) -> dict[str, int]:
        """Async entry for scraping after name resolution, reused by the job worker."""
        from libref.services.registry.resolver import resolve_package

        async with _client(self._settings) as client:
            resolution = await resolve_package(
                self._conn,
                client,
                self._settings,
                ecosystem,
                package,
                requested_version,
                include_prerelease=include_prerelease,
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
            except Exception:  # noqa: BLE001, S110 — heartbeat only aims to renew
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
                "scope_content_type_rejected",
                "seed_truncated",
                "snapshot_written",
                "snapshot_quota_skipped",
                "deep_discovered",
                "deep_truncated",
                "elapsed_ms",
                *(f"{step}_ms" for step in PIPELINE_STEPS),
            )
        }
        async with _version_lock(self._conn, ecosystem, library, version):
            removed_all: list[int] = []
            inserted_all: list[int] = []
            async with _client(self._settings) as client:
                robots = RobotsChecker(
                    self._conn,
                    client,
                    ttl_days=self._settings.robots_cache_ttl_days,
                    user_agent=self._settings.user_agent,
                )
                seeder = UrlSeeder(
                    client,
                    robots,
                    cache_ttl_hours=self._settings.seeder_cache_ttl_hours,
                )
                fetcher = HttpFetcher(
                    client,
                    max_retries=self._settings.fetch_max_retries,
                    default_delay=self._settings.crawl_delay_default,
                )
                discover_start = _time.monotonic()
                # Refresh intent re-discovers: reusing seeds within TTL would hide
                # added or removed pages from the sitemap on every refresh.
                force_seed = bool((scope or {}).get("refresh_seed", False)) or (
                    resolved_from == "refresh"
                )
                seed = await seeder.discover(
                    start_url,
                    prefilter=prefilter,
                    max_pages=None,
                    query=(scope or {}).get("crawl_query"),
                    score_threshold=(scope or {}).get("score_threshold"),
                    force=force_seed,
                )
                counts["discover_ms"] = int((_time.monotonic() - discover_start) * 1000)
                accepted, rejected, truncated, plan_stats = _plan_targets(
                    seed, start_url, scope, max_pages
                )
                counts["skipped"] += len(rejected)
                counts["scope_rejected"] += len(rejected)
                counts["scope_content_type_rejected"] += int(
                    plan_stats.content_type_rejected
                )
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
                follow = bool(
                    (scope or {}).get("follow_links", False)
                    or self._settings.follow_links_default
                )
                policy = build_scope(start_url, scope)
                visited: set[str] = set(accepted)
                pending: list[tuple[str, int]] = []

                async def process(url: str, depth: int) -> None:
                    async with sem:
                        try:
                            page = store.get_page(version_id, url)
                            (
                                result,
                                empty_dropped,
                                render_skipped,
                                stage_ms,
                                detail,
                            ) = await self._fetch_and_store(
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
                        except Exception as exc:  # noqa: BLE001 — a single page's failure is recorded as an outcome and does not block the batch
                            logger.debug("page failed url=%s: %s", url, exc)
                            result, empty_dropped, render_skipped, stage_ms, detail = (
                                StoreResult("failed"),
                                0,
                                False,
                                {},
                                _FetchDetail(),
                            )
                        _record_outcome(
                            counts,
                            removed_all,
                            inserted_all,
                            result,
                            empty_dropped,
                            render_skipped,
                            stage_ms,
                        )
                        if detail.canonical and detail.canonical != url:
                            visited.add(detail.canonical)
                        if follow:
                            for link in detail.links:
                                if link in visited:
                                    continue
                                if (
                                    policy.max_depth is not None
                                    and depth + 1 > policy.max_depth
                                ):
                                    counts["deep_truncated"] = 1
                                    continue
                                if not policy.accept(link):
                                    continue
                                visited.add(link)
                                pending.append((link, depth + 1))
                        _beat()

                await asyncio.gather(*(process(u, 0) for u in accepted))
                if follow:
                    while pending:
                        if max_pages is not None and len(visited) >= max_pages:
                            counts["deep_truncated"] = 1
                            break
                        batch = pending
                        pending = []
                        if max_pages is not None:
                            room = max_pages - len(visited)
                            if len(batch) > room:
                                counts["deep_truncated"] = 1
                                batch = batch[:room]
                        counts["deep_discovered"] += len(batch)
                        await asyncio.gather(*(process(u, d) for u, d in batch))
                        _beat()
                # Absent sweeping is seed-driven; link-followed pages are outside
                # the seed set, so the sweep is skipped in follow mode entirely.
                if not follow:
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
                await self._sync_vectors(
                    store, counts, removed_all, inserted_all, version_id=version_id
                )
                counts["vectors_ms"] = int((_time.monotonic() - vectors_start) * 1000)
                _beat()
            import time as _time2

            counts["seed_truncated"] = 1 if truncated else 0
            counts["elapsed_ms"] = int((_time2.monotonic() - started) * 1000)
            if counts.get("render_skipped", 0) > 0:
                scope_render = bool(
                    (scope or {}).get("render_js", False)
                    or bool(self._settings.render_js_default)
                )
                if not scope_render:
                    logger.warning(
                        "render candidates skipped=%d without browser fallback; enable render_js to rescan",
                        counts.get("render_skipped", 0),
                    )
            logger.info(
                "scrape done %s:%s %s stored=%d unchanged=%d absent=%d failed=%d skipped=%d",
                ecosystem,
                library,
                version,
                counts.get("stored", 0),
                counts.get("unchanged", 0),
                counts.get("absent", 0),
                counts.get("failed", 0),
                counts.get("skipped", 0),
            )
            return counts

    async def _sync_vectors(
        self,
        store: DocumentStore,
        counts: dict[str, int],
        removed: list[int],
        inserted: list[int],
        *,
        version_id: int | None = None,
    ) -> None:
        from libref.services.ops.maintenance import sync_vectors_for_docs

        await sync_vectors_for_docs(
            self._conn,
            self._settings,
            self._vec,
            self._embed,
            store,
            counts,
            removed,
            inserted,
            version_id=version_id,
        )

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
                from libref.services.ops.retention import (
                    REASON_ABSENT_SWEEP,
                    log_deletion,
                )

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
                logger.info(
                    "absent sweep %s:%s %s urls=%d docs=%d",
                    ecosystem,
                    library,
                    version,
                    len(removed_urls),
                    len(removed),
                )
            except Exception as exc:  # noqa: BLE001 — deletion-log failure does not overturn the source-of-truth deletion
                logger.debug("absent log failed: %s", exc)
        return removed

    def _clean_with_cache(self, kind: str, raw: str, url: str) -> str:
        """Cleaning cache: same content and same budget fingerprint hit reuses directly; evict the oldest entry over the cap."""
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
            cleaned = self._scrape_cached(raw, url).markdown
        while len(self._clean_cache) >= self._settings.ingest_clean_cache_cap:
            oldest = next(iter(self._clean_cache))
            del self._clean_cache[oldest]
        self._clean_cache[digest] = cleaned
        return cleaned

    def _scrape_cached(self, raw: str, url: str) -> ScrapeResult:
        """Structured scrape cache: same content and same budget fingerprint hit reuses directly."""
        import hashlib as _hashlib

        digest = _hashlib.sha1(
            f"html\0{self._clean_fingerprint}\0{raw}".encode("utf-8", "ignore")
        ).hexdigest()
        hit = self._scrape_cache.get(digest)
        if hit is not None:
            return hit
        try:
            scraped = scrape_html(raw, url)
        except Exception:  # noqa: BLE001
            scraped = ScrapeResult()
        while len(self._scrape_cache) >= self._settings.ingest_clean_cache_cap:
            oldest = next(iter(self._scrape_cache))
            del self._scrape_cache[oldest]
        self._scrape_cache[digest] = scraped
        return scraped

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
    ) -> tuple[StoreResult, int, bool, dict[str, int], _FetchDetail]:
        import time as _time

        stage_ms: dict[str, int] = {f"{step}_ms": 0 for step in PIPELINE_STEPS}
        stage_ms["snapshot_written"] = 0
        stage_ms["snapshot_quota_skipped"] = 0
        fetch_start = _time.monotonic()
        if not await robots.can_fetch(url):
            return StoreResult("skipped"), 0, False, stage_ms, _FetchDetail()
        delay = await robots.crawl_delay(url)
        cache_mode = "bypass" if (scope or {}).get("bypass_cache", False) else "enabled"
        result = await fetcher.fetch(
            url,
            etag=page.etag if page else None,
            last_modified=page.last_modified if page else None,
            crawl_delay=delay,
            cache_mode=cache_mode,  # type: ignore[arg-type]
        )
        if result.status == "unchanged":
            stage_ms["fetch_ms"] = int((_time.monotonic() - fetch_start) * 1000)
            return (
                store.store_page(version_id, library_id, url, "unchanged"),
                0,
                False,
                stage_ms,
                _FetchDetail(),
            )
        if result.status == "absent":
            stage_ms["fetch_ms"] = int((_time.monotonic() - fetch_start) * 1000)
            return (
                store.store_page(version_id, library_id, url, "absent"),
                0,
                False,
                stage_ms,
                _FetchDetail(),
            )
        if result.status == "failed":
            stage_ms["fetch_ms"] = int((_time.monotonic() - fetch_start) * 1000)
            return (
                store.store_page(version_id, library_id, url, "failed"),
                0,
                False,
                stage_ms,
                _FetchDetail(),
            )
        if self._snapshots is not None and snapshot_ctx is not None and result.content:
            try:
                ecosystem, library, version = snapshot_ctx
                _, written = self._snapshots.put(
                    ecosystem,
                    library,
                    version,
                    url,
                    result.content.encode("utf-8", errors="ignore"),
                    result.content_type,
                )
                if written:
                    stage_ms["snapshot_written"] = 1
                else:
                    stage_ms["snapshot_quota_skipped"] = 1
            except Exception:  # noqa: BLE001, S110 — snapshot quota bookkeeping must not abort ingestion
                pass

        raw_body = result.content or ""
        kind = classify_content(result.content_type, url)
        clean_start = _time.monotonic()
        detail = _FetchDetail()
        if kind == "html":
            scraped = self._scrape_cached(raw_body, url)
            markdown = scraped.markdown
            detail = _FetchDetail(
                links=[]
                if scraped.nofollow
                else scraped.internal_links + scraped.external_links,
                canonical=scraped.canonical,
            )
            if scraped.noindex:
                stage_ms["fetch_ms"] = int((clean_start - fetch_start) * 1000)
                stage_ms["clean_ms"] = int((_time.monotonic() - clean_start) * 1000)
                return StoreResult("skipped"), 0, False, stage_ms, _FetchDetail()
        else:
            markdown = self._clean_with_cache(kind, raw_body, url)
        render_attempted = False
        if not markdown:
            if looks_like_js_rendered(raw_body, markdown):
                render_attempted = True
                want_render = bool(
                    (scope or {}).get("render_js", False)
                    or self._settings.render_js_default
                )
                if want_render:
                    try:
                        from libref.crawler.browser import (
                            is_playwright_available,
                            render_html,
                        )

                        wait_for = (scope or {}).get("render_wait_for") or (
                            self._settings.render_wait_for
                        )
                        timeout_ms = (scope or {}).get("render_timeout_ms") or (
                            self._settings.render_timeout_ms
                        )
                        rendered = (
                            await render_html(
                                url, timeout_ms=int(timeout_ms), wait_for=wait_for
                            )
                            if is_playwright_available()
                            else None
                        )
                    except Exception:  # noqa: BLE001
                        rendered = None
                    if rendered:
                        rescraped = self._scrape_cached(rendered, url)
                        markdown = rescraped.markdown
                        detail = _FetchDetail(
                            links=[]
                            if rescraped.nofollow
                            else rescraped.internal_links + rescraped.external_links,
                            canonical=rescraped.canonical,
                        )
                if not markdown:
                    stage_ms["fetch_ms"] = int((clean_start - fetch_start) * 1000)
                    stage_ms["clean_ms"] = int((_time.monotonic() - clean_start) * 1000)
                    return (
                        StoreResult("skipped"),
                        0,
                        render_attempted,
                        stage_ms,
                        _FetchDetail(),
                    )
            stage_ms["fetch_ms"] = int((clean_start - fetch_start) * 1000)
            stage_ms["clean_ms"] = int((_time.monotonic() - clean_start) * 1000)
            return (
                store.store_page(
                    version_id,
                    library_id,
                    url,
                    "empty",
                    raw_content=result.content,
                    etag=result.etag,
                    last_modified=result.last_modified,
                ),
                0,
                render_attempted,
                stage_ms,
                _FetchDetail(),
            )
        stage_ms["fetch_ms"] = int((clean_start - fetch_start) * 1000)
        clean_end = _time.monotonic()
        stage_ms["clean_ms"] = int((clean_end - clean_start) * 1000)
        store_start = clean_end
        outcome = store.ensure_changed(version_id, url, markdown)
        if outcome == "unchanged":
            stage_ms["store_ms"] = int((_time.monotonic() - store_start) * 1000)
            return (
                store.store_page(version_id, library_id, url, "unchanged"),
                0,
                render_attempted,
                stage_ms,
                detail,
            )
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
        return stored, dual.empty_dropped, render_attempted, stage_ms, detail
