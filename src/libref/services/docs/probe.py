"""Read-only documentation probe: discovery shape without fetching bodies.

Runs the same seeder and scope filter as the ingest path but stops before
any page fetch, clean, or store. Used by agents and humans to confirm the
range before submitting a scrape job.
"""

from __future__ import annotations

import asyncio
import json as _json
import re
import time as _time
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from libref.crawler import RobotsChecker, UrlSeeder
from libref.services.docs.scrape_plan import plan_targets


class PrefixGroup(BaseModel):
    prefix: str
    count: int


class SampleFetch(BaseModel):
    url: str
    status: int | None = None
    content_type: str = ""
    title: str = ""
    error: str = ""


class DocProbe(BaseModel):
    start_url: str
    source: str
    total_discovered: int
    accepted: int
    rejected: int
    truncated: bool
    failed: bool
    cross_site: int
    max_pages: int | None = None
    budget_exceeded: bool = False
    follow_links: bool = False
    follow_expansion_unknown: bool = False
    prefix_groups: list[PrefixGroup] = []
    samples: list[str] = []
    sample_details: list[SampleFetch] = []
    errors: dict[str, str] = {}


_DOC_PROBE_CACHE: dict[str, tuple[float, DocProbe]] = {}
_DOC_PROBE_CACHE_CAP = 64


def _doc_cache_key(
    start_url: str,
    scope: dict[str, Any] | None,
    limit: int,
    fetch_limit: int,
    max_pages: int | None,
    prefilter: bool,
) -> str:
    try:
        scope_text = _json.dumps(
            scope or {}, sort_keys=True, ensure_ascii=False, default=str
        )
    except TypeError, ValueError:
        scope_text = str(scope)
    return "\0".join(
        (start_url, scope_text, str(limit), str(fetch_limit), str(max_pages), str(bool(prefilter)))
    )


def _doc_cache_get(key: str, ttl_hours: float) -> DocProbe | None:
    hit = _DOC_PROBE_CACHE.get(key)
    if hit is None:
        return None
    stored_at, probe = hit
    if ttl_hours <= 0 or _time.time() - stored_at > ttl_hours * 3600:
        _DOC_PROBE_CACHE.pop(key, None)
        return None
    return probe


def _doc_cache_put(key: str, probe: DocProbe) -> None:
    while len(_DOC_PROBE_CACHE) >= _DOC_PROBE_CACHE_CAP:
        oldest = next(iter(_DOC_PROBE_CACHE))
        del _DOC_PROBE_CACHE[oldest]
    _DOC_PROBE_CACHE[key] = (_time.time(), probe)


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_HEAD_READ_LIMIT = 65536


def _title_of(head: bytes) -> str:
    text = head.decode("utf-8", "ignore")
    match = _TITLE_RE.search(text)
    if match is None:
        return ""
    return " ".join(match.group(1).split())


async def _fetch_sample_heads(
    client: httpx.AsyncClient, urls: list[str], *, timeout_s: float
) -> list[SampleFetch]:
    """Bounded head read per URL; per-URL failures are recorded, never raised."""
    slots: list[SampleFetch | None] = [None] * len(urls)
    semaphore = asyncio.Semaphore(4)

    async def _one(index: int, url: str) -> None:
        async with semaphore:
            try:
                async with client.stream(
                    "GET",
                    url,
                    headers={"Range": "bytes=0-65535"},
                    timeout=timeout_s,
                    follow_redirects=True,
                ) as response:
                    content_type = (
                        response.headers.get("content-type", "").split(";")[0].strip()
                    )
                    head = b""
                    async for chunk in response.aiter_bytes(8192):
                        head += chunk
                        if len(head) >= _HEAD_READ_LIMIT:
                            break
                title = _title_of(head) if "html" in content_type else ""
                slots[index] = SampleFetch(
                    url=url,
                    status=response.status_code,
                    content_type=content_type,
                    title=title,
                )
            except Exception as exc:  # noqa: BLE001 — per-URL failures are recorded, never raised
                slots[index] = SampleFetch(url=url, error=f"{type(exc).__name__}: {exc}")

    await asyncio.gather(*(_one(i, u) for i, u in enumerate(urls)))
    return [item if item is not None else SampleFetch(url=url) for item, url in zip(slots, urls)]


def _prefix_groups(urls: list[str], limit: int = 20) -> list[PrefixGroup]:
    counts: dict[str, int] = {}
    for url in urls:
        try:
            path = urlsplit(url).path or "/"
        except ValueError:
            continue
        segments = [s for s in path.split("/") if s]
        if not segments:
            key = "/"
        elif len(segments) == 1:
            key = segments[0]
        else:
            key = "/".join(segments[:2])
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [PrefixGroup(prefix=k, count=v) for k, v in ranked[:limit]]


async def probe_docs_async(
    conn,
    settings,
    start_url: str,
    *,
    scope: dict[str, Any] | None = None,
    sample_limit: int | None = None,
    fetch_sample_limit: int | None = None,
    max_pages: int | None = None,
    prefilter: bool = False,
) -> DocProbe:
    """Discover and filter only, plus an optional bounded head fetch per sample.

    Never writes domain tables; the head fetch exists only to confirm titles
    and content types before submitting a scrape job.
    """
    from libref.crawler.http import build_async_client

    if not start_url.strip():
        raise ValueError("start_url is empty")
    if max_pages is not None and (not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 1):
        raise ValueError("max_pages must be a positive integer")
    limit = sample_limit if sample_limit is not None else settings.probe_sample_limit
    fetch_total = (
        fetch_sample_limit
        if fetch_sample_limit is not None
        else settings.probe_fetch_sample_limit
    )
    cache_key = _doc_cache_key(start_url, scope, int(limit), int(fetch_total), max_pages, prefilter)
    cached = _doc_cache_get(
        cache_key, float(getattr(settings, "probe_cache_ttl_hours", 1.0) or 0.0)
    )
    if cached is not None:
        return cached
    effective_scope = dict(scope or {})
    follow = bool(
        effective_scope.get("follow_links", False)
        or getattr(settings, "follow_links_default", False)
    )
    async with build_async_client(settings) as client:
        robots = RobotsChecker(
            conn,
            client,
            ttl_days=settings.robots_cache_ttl_days,
            user_agent=settings.user_agent,
        )
        seeder = UrlSeeder(
            client,
            robots,
            cache_ttl_hours=settings.probe_cache_ttl_hours,
        )
        prefixes = tuple(effective_scope.get("path_prefixes") or ())
        seed = await seeder.discover(
            start_url,
            prefilter=prefilter,
            max_pages=None,
            query=effective_scope.get("crawl_query"),
            score_threshold=effective_scope.get("score_threshold"),
            path_prefixes=prefixes or None,
        )
        accepted, rejected, truncated, _stats = plan_targets(
            seed, start_url, effective_scope, None
        )
        fetch_urls = list(accepted[: max(0, int(limit))])[: max(0, int(fetch_total))]
        if fetch_urls:
            details = await _fetch_sample_heads(
                client,
                fetch_urls,
                timeout_s=float(getattr(settings, "probe_fetch_timeout", 10.0) or 10.0),
            )
        else:
            details = []
    budget_exceeded = max_pages is not None and len(accepted) > max_pages
    probe = DocProbe(
        start_url=start_url,
        source=seed.source,
        total_discovered=len(seed.urls),
        accepted=len(accepted),
        rejected=len(rejected),
        truncated=bool(truncated or seed.truncated),
        failed=bool(seed.failed),
        cross_site=len(seed.cross_site),
        max_pages=max_pages,
        budget_exceeded=bool(budget_exceeded),
        follow_links=follow,
        follow_expansion_unknown=follow,
        prefix_groups=_prefix_groups(accepted),
        samples=list(accepted[: max(0, int(limit))]),
        sample_details=details,
        errors=dict(seed.errors),
    )
    _doc_cache_put(cache_key, probe)
    return probe


def probe_docs(
    conn,
    settings,
    start_url: str,
    *,
    scope: dict[str, Any] | None = None,
    sample_limit: int | None = None,
    fetch_sample_limit: int | None = None,
    max_pages: int | None = None,
    prefilter: bool = False,
) -> DocProbe:
    import asyncio

    return asyncio.run(
        probe_docs_async(
            conn,
            settings,
            start_url,
            scope=scope,
            sample_limit=sample_limit,
            fetch_sample_limit=fetch_sample_limit,
            max_pages=max_pages,
            prefilter=prefilter,
        )
    )
