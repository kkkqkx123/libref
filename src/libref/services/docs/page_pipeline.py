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

    def __init__(self, settings, tenant_id: str = "default") -> None:
        self._settings = settings
        self._tenant_id = tenant_id
        self._budgets = doc_budgets(settings)
        self._clean_fingerprint = budget_fingerprint(self._budgets)
        self._clean_cache: dict[str, str] = {}
        self._scrape_cache: dict[str, ScrapeResult] = {}
        self._snapshots = None
        if bool(settings.snapshot_enabled):
            from libref.services.infra.tenant import tenant_snapshot_dir as _tsd
            from libref.store.snapshots import SnapshotStore

            try:
                _root = _tsd(settings, tenant_id)
            except Exception:  # noqa: BLE001
                _root = Path(settings.snapshot_dir)
            self._snapshots = SnapshotStore(
                _root,
                quota_mb=int(settings.snapshot_quota_mb),
            )

    def set_tenant(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id
        if bool(self._settings.snapshot_enabled):
            from libref.services.infra.tenant import tenant_snapshot_dir as _tsd2
            from libref.store.snapshots import SnapshotStore

            try:
                _root2 = _tsd2(self._settings, tenant_id)
            except Exception:  # noqa: BLE001
                from pathlib import Path as _P

                _root2 = _P(self._settings.snapshot_dir)
            self._snapshots = SnapshotStore(
                _root2, quota_mb=int(self._settings.snapshot_quota_mb)
            )

    def _gate_snapshot_write(self, store, tenant_id: str, size: int) -> bool:
        """Return False when the subject is over snapshot_bytes quota; records the denial."""
        if not bool(getattr(self._settings, "quota_enabled", False)):
            return True
        try:
            from libref.services.infra import metrics as _metrics
            from libref.services.infra import quota as _quota

            conn = getattr(store, "_conn", None)
            if conn is None:
                return True
            decision = _quota.check(
                conn,
                tenant_id=tenant_id,
                principal_id=None,
                resource="snapshot_bytes",
                cost=size,
                settings=self._settings,
            )
            if not decision.allowed:
                _metrics.incr("quota_denied_total{resource=snapshot_bytes}")
                _quota.record_event(
                    conn,
                    tenant_id=tenant_id,
                    principal_id=None,
                    resource="snapshot_bytes",
                    result="denied",
                    used=decision.used,
                    limit_value=decision.limit_value,
                )
                return False
        except Exception:  # noqa: BLE001 — quota gating must never abort ingestion
            return True
        return True

    def _account_page(self, store, tenant_id: str, *, snapshot_bytes: int) -> None:
        """Accumulate scrape_pages and written snapshot bytes for the tenant; best effort."""
        if not bool(getattr(self._settings, "quota_enabled", False)):
            return
        try:
            from libref.services.infra import quota as _quota

            conn = getattr(store, "_conn", None)
            if conn is None:
                return
            _quota.add_usage(
                conn, tenant_id=tenant_id, principal_id=None, resource="scrape_pages"
            )
            if snapshot_bytes > 0:
                _quota.add_usage(
                    conn,
                    tenant_id=tenant_id,
                    principal_id=None,
                    resource="snapshot_bytes",
                    amount=snapshot_bytes,
                )
        except Exception:  # noqa: BLE001, S110 — metering must never abort ingestion
            pass

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
        snapshot_ctx: tuple | None = None,
        tenant_id: str = "default",
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
                store.store_page(
                    version_id, library_id, url, "unchanged", tenant_id=tenant_id
                ),
                0,
                False,
                stage_ms,
                FetchDetail(),
            )
        if result.status == "absent":
            stage_ms["fetch_ms"] = int((_time.monotonic() - fetch_start) * 1000)
            return (
                store.store_page(
                    version_id, library_id, url, "absent", tenant_id=tenant_id
                ),
                0,
                False,
                stage_ms,
                FetchDetail(),
            )
        if result.status == "failed":
            stage_ms["fetch_ms"] = int((_time.monotonic() - fetch_start) * 1000)
            return (
                store.store_page(
                    version_id, library_id, url, "failed", tenant_id=tenant_id
                ),
                0,
                False,
                stage_ms,
                FetchDetail(),
            )
        if self._snapshots is not None and snapshot_ctx is not None and result.content:
            try:
                if len(snapshot_ctx) == 4:
                    ecosystem, library, version, _t = snapshot_ctx
                else:
                    ecosystem, library, version = snapshot_ctx
                body = result.content.encode("utf-8", errors="ignore")
                if not self._gate_snapshot_write(store, tenant_id, len(body)):
                    stage_ms["snapshot_quota_skipped"] = 1
                else:
                    _, written = self._snapshots.put(
                        ecosystem,
                        library,
                        version,
                        url,
                        body,
                        result.content_type,
                    )
                    if written:
                        stage_ms["snapshot_written"] = 1
                        self._account_page(store, tenant_id, snapshot_bytes=len(body))
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
                external_links=[] if scraped.nofollow else list(scraped.external_links),
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
                            external_links=[]
                            if rescraped.nofollow
                            else list(rescraped.external_links),
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
                    tenant_id=tenant_id,
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
                store.store_page(
                    version_id, library_id, url, "unchanged", tenant_id=tenant_id
                ),
                0,
                render_attempted,
                stage_ms,
                detail,
            )
        if bool(getattr(self._settings, "usage_mining_enabled", True)) and bool(
            getattr(self._settings, "doc_code_mining_enabled", True)
        ):
            conn = getattr(store, "_conn", None)
            if conn is not None:
                from libref.astminer.code_blocks import fenced_blocks
                from libref.crawler.code_blocks import html_code_blocks
                from libref.services.code.usage_ingest import ingest_snippets

                meta = conn.execute(
                    "SELECT l.name, v.version FROM versions v"
                    " JOIN libraries l ON l.id=v.library_id WHERE v.id=?",
                    (version_id,),
                ).fetchone()
                if meta is not None:
                    prefix = f"doc:{version_id}:{url}"
                    if kind == "html":
                        blocks = html_code_blocks(
                            scraped.cleaned_html,
                            sample_prefix=prefix,
                            url=url,
                            heading=scraped.title,
                        )
                        origin = "html_code_block"
                    else:
                        blocks = fenced_blocks(markdown, sample_prefix=prefix, url=url)
                        origin = "markdown_fence"
                    if blocks:
                        ingest_snippets(
                            conn,
                            str(meta[0]),
                            str(meta[1]),
                            blocks,
                            source_key=f"doc:{version_id}",
                            source_role="doc_snippet",
                            origin_kind=origin,
                            tenant_id=self._tenant_id,
                            source_url=url,
                            settings=self._settings,
                        )
        split_start = _time.monotonic()
        self._account_page(store, tenant_id, snapshot_bytes=0)
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
            tenant_id=tenant_id,
        )
        stage_ms["store_ms"] = int((_time.monotonic() - split_end) * 1000)
        return stored, dual.empty_dropped, render_attempted, stage_ms, detail
