"""Single-page pipeline: fetch, clean, chunk, store."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from libref.chunking import split_markdown_dual
from libref.crawler import (
    HttpFetcher,
    RobotsChecker,
    looks_like_js_rendered,
    scrape_html,
)
from libref.crawler.extract import ScrapeResult
from libref.services.docs.scrape_plan import (
    PIPELINE_STEPS,
    FetchDetail,
    budget_fingerprint,
    build_doc_inputs,
    doc_budgets,
    normalize_text_content,
)
from libref.store.document_store import DocumentStore, StoreResult
from libref.text.normalize import classify_content, wrap_source_content

logger = logging.getLogger(__name__)


class PagePipeline:
    """Owns chunk budgets, cleaning caches, and snapshots for one scrape."""

    def __init__(self, settings) -> None:
        self._settings = settings
        self._budgets = doc_budgets(settings)
        self._clean_fingerprint = budget_fingerprint(self._budgets)
        self._clean_cache: dict[str, str] = {}
        self._scrape_cache: dict[str, ScrapeResult] = {}
        self._snapshots = None
        if bool(settings.snapshot_enabled):
            from libref.store.snapshots import SnapshotStore

            self._snapshots = SnapshotStore(
                Path(settings.snapshot_dir),
                quota_mb=int(settings.snapshot_quota_mb),
            )

    def clean_with_cache(self, kind: str, raw: str, url: str) -> str:
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
            cleaned = self.scrape_cached(raw, url).markdown
        while len(self._clean_cache) >= self._settings.ingest_clean_cache_cap:
            oldest = next(iter(self._clean_cache))
            del self._clean_cache[oldest]
        self._clean_cache[digest] = cleaned
        return cleaned

    def scrape_cached(self, raw: str, url: str) -> ScrapeResult:
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

    async def fetch_and_store(
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
    ) -> tuple[StoreResult, int, bool, dict[str, int], FetchDetail]:
        import time as _time

        stage_ms: dict[str, int] = {f"{step}_ms": 0 for step in PIPELINE_STEPS}
        stage_ms["snapshot_written"] = 0
        stage_ms["snapshot_quota_skipped"] = 0
        fetch_start = _time.monotonic()
        if not await robots.can_fetch(url):
            return StoreResult("skipped"), 0, False, stage_ms, FetchDetail()
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
                FetchDetail(),
            )
        if result.status == "absent":
            stage_ms["fetch_ms"] = int((_time.monotonic() - fetch_start) * 1000)
            return (
                store.store_page(version_id, library_id, url, "absent"),
                0,
                False,
                stage_ms,
                FetchDetail(),
            )
        if result.status == "failed":
            stage_ms["fetch_ms"] = int((_time.monotonic() - fetch_start) * 1000)
            return (
                store.store_page(version_id, library_id, url, "failed"),
                0,
                False,
                stage_ms,
                FetchDetail(),
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
        detail = FetchDetail()
        if kind == "html":
            scraped = self.scrape_cached(raw_body, url)
            markdown = scraped.markdown
            detail = FetchDetail(
                links=[]
                if scraped.nofollow
                else scraped.internal_links + scraped.external_links,
                canonical=scraped.canonical,
                structure_links=[]
                if scraped.nofollow
                else list(scraped.structure_links),
            )
            if scraped.noindex:
                stage_ms["fetch_ms"] = int((clean_start - fetch_start) * 1000)
                stage_ms["clean_ms"] = int((_time.monotonic() - clean_start) * 1000)
                return StoreResult("skipped"), 0, False, stage_ms, FetchDetail()
        else:
            markdown = self.clean_with_cache(kind, raw_body, url)
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
                        rescraped = self.scrape_cached(rendered, url)
                        markdown = rescraped.markdown
                        detail = FetchDetail(
                            links=[]
                            if rescraped.nofollow
                            else rescraped.internal_links + rescraped.external_links,
                            canonical=rescraped.canonical,
                            structure_links=[]
                            if rescraped.nofollow
                            else list(rescraped.structure_links),
                        )
                if not markdown:
                    stage_ms["fetch_ms"] = int((clean_start - fetch_start) * 1000)
                    stage_ms["clean_ms"] = int((_time.monotonic() - clean_start) * 1000)
                    return (
                        StoreResult("skipped"),
                        0,
                        render_attempted,
                        stage_ms,
                        FetchDetail(),
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
                FetchDetail(),
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
        inputs = build_doc_inputs(raw_chunks, result.content_type, kind)
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
