"""Scrape orchestration: discover URLs, fetch, clean, chunk, store, sync vectors.

The orchestration layer is the only scheduling location; exit layers only call this module. Vectors are a derived index:
vectors are synced only after the source-of-truth store succeeds; vector failures are only counted and never overturn the source write.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from libref.crawler import HttpFetcher, RobotsChecker, UrlSeeder, build_scope
from libref.llm import EmbedFn
from libref.services.docs.scrape_jobs import (
    SCRAPE_JOB_KIND,
    build_scrape_runner,
    run_scrape_job_sync,
    scrape_job_params,
    start_refresh_job,
    start_scrape_job,
)
from libref.services.docs.scrape_plan import (
    PIPELINE_STEPS,
    FetchDetail,
    plan_targets,
    record_outcome,
)
from libref.services.docs.scrape_sweep import sweep_absent, sync_vectors
from libref.services.infra.version_lock import version_lock as _version_lock
from libref.services.registry.libraries import LibraryService
from libref.store.document_store import DocumentStore, StoreResult
from libref.store.vec_store import VectorStore

logger = logging.getLogger(__name__)

__all__ = [
    "PIPELINE_STEPS",
    "SCRAPE_JOB_KIND",
    "FetchDetail",
    "IngestService",
    "build_scrape_runner",
    "plan_targets",
    "record_outcome",
    "run_scrape_job_sync",
    "scrape_job_params",
    "start_refresh_job",
    "start_scrape_job",
    "sweep_absent",
    "sync_vectors",
]


@asynccontextmanager
async def _client(settings) -> AsyncIterator[httpx.AsyncClient]:
    from libref.crawler.http import build_async_client

    async with build_async_client(settings) as client:
        yield client


class IngestService:
    def __init__(
        self,
        settings,
        conn,
        *,
        vec_store: VectorStore | None = None,
        embed: EmbedFn | None = None,
    ) -> None:
        from libref.services.docs.page_pipeline import PagePipeline
        from libref.store.vec_store import paired_vector

        self._settings = settings
        self._conn = conn
        self._vec, self._embed = paired_vector(vec_store, embed)
        self._pipeline = PagePipeline(settings)

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
        fresh: bool = False,
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
                fresh=fresh,
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
        fresh: bool = False,
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
            fresh=fresh,
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
        fresh: bool = False,
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
            fresh=fresh,
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
        fresh: bool = False,
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
            fresh=fresh,
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
        fresh: bool = False,
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
                "resumed_skipped",
                "batches",
                "elapsed_ms",
                *(f"{step}_ms" for step in PIPELINE_STEPS),
            )
        }
        _stale_after = float(
            getattr(self._settings, "heartbeat_timeout", 600.0) or 600.0
        )
        async with _version_lock(
            self._conn, ecosystem, library, version, stale_after=_stale_after
        ):
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
                accepted, rejected, truncated, plan_stats = plan_targets(
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
                            ) = await self._pipeline.fetch_and_store(
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
                                FetchDetail(),
                            )
                        record_outcome(
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

                from libref.services.infra.progress import (
                    DOCS_KIND,
                    clear_progress,
                    resume_offset,
                    save_progress,
                )

                batch_size = max(
                    1, int(getattr(self._settings, "ingest_batch_pages", 200) or 200)
                )
                done, fingerprint = resume_offset(
                    self._conn, version_id, DOCS_KIND, accepted, fresh=fresh
                )
                counts["resumed_skipped"] = done
                remaining = accepted[done:]
                for start in range(0, len(remaining), batch_size):
                    batch_urls = remaining[start : start + batch_size]
                    prev_removed = len(removed_all)
                    prev_inserted = len(inserted_all)
                    await asyncio.gather(*(process(u, 0) for u in batch_urls))
                    counts["batches"] += 1
                    vectors_start = _time.monotonic()
                    await sync_vectors(
                        self._conn,
                        self._settings,
                        self._vec,
                        self._embed,
                        store,
                        counts,
                        removed_all[prev_removed:],
                        inserted_all[prev_inserted:],
                        version_id=version_id,
                    )
                    counts["vectors_ms"] += int(
                        (_time.monotonic() - vectors_start) * 1000
                    )
                    save_progress(
                        self._conn,
                        version_id,
                        DOCS_KIND,
                        total=len(accepted),
                        done=done + start + len(batch_urls),
                        fingerprint=fingerprint,
                        batch_size=batch_size,
                    )
                    _beat()
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
                        for start in range(0, len(batch), batch_size):
                            await asyncio.gather(
                                *(
                                    process(u, d)
                                    for u, d in batch[start : start + batch_size]
                                )
                            )
                            _beat()
                # Absent sweeping is seed-driven; link-followed pages are outside
                # the seed set, so the sweep is skipped in follow mode entirely.
                # Follow-discovered pages also sit outside the resume cursor and
                # are re-walked on retry.
                # Batch syncs already pushed page deltas with the version watermark;
                # the closing sync only carries sweep deletions plus set verification.
                sweep_prev = len(removed_all)
                if not follow:
                    removed_all.extend(
                        sweep_absent(
                            self._conn,
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
                await sync_vectors(
                    self._conn,
                    self._settings,
                    self._vec,
                    self._embed,
                    store,
                    counts,
                    removed_all[sweep_prev:],
                    [],
                    version_id=version_id,
                    repair=False,
                )
                counts["vectors_ms"] += int((_time.monotonic() - vectors_start) * 1000)
                clear_progress(self._conn, version_id, DOCS_KIND)
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
