"""Read-only documentation probe: discovery shape without fetching bodies.

Runs the same seeder and scope filter as the ingest path but stops before
any page fetch, clean, or store. Used by agents and humans to confirm the
range before submitting a scrape job.
"""

from __future__ import annotations

import json as _json
import time as _time
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel

from libref.crawler import RobotsChecker, UrlSeeder
from libref.services.docs.scrape_plan import plan_targets


class PrefixGroup(BaseModel):
    prefix: str
    count: int


class DocProbe(BaseModel):
    start_url: str
    source: str
    total_discovered: int
    accepted: int
    rejected: int
    truncated: bool
    failed: bool
    cross_site: int
    prefix_groups: list[PrefixGroup] = []
    samples: list[str] = []
    errors: dict[str, str] = {}


_DOC_PROBE_CACHE: dict[str, tuple[float, DocProbe]] = {}
_DOC_PROBE_CACHE_CAP = 64


def _doc_cache_key(start_url: str, scope: dict[str, Any] | None, limit: int) -> str:
    try:
        scope_text = _json.dumps(
            scope or {}, sort_keys=True, ensure_ascii=False, default=str
        )
    except TypeError, ValueError:
        scope_text = str(scope)
    return "\0".join((start_url, scope_text, str(limit)))


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


def _prefix_groups(urls: list[str], limit: int = 20) -> list[PrefixGroup]:
    counts: dict[str, int] = {}
    for url in urls:
        try:
            path = urlsplit(url).path or "/"
        except ValueError:
            continue
        segments = [s for s in path.split("/") if s]
        key = segments[0] if segments else "/"
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
) -> DocProbe:
    """Discover and filter only; never fetches page bodies or writes domain tables."""
    from libref.crawler.http import build_async_client

    if not start_url.strip():
        raise ValueError("start_url is empty")
    limit = sample_limit if sample_limit is not None else settings.probe_sample_limit
    cache_key = _doc_cache_key(start_url, scope, int(limit))
    cached = _doc_cache_get(
        cache_key, float(getattr(settings, "probe_cache_ttl_hours", 1.0) or 0.0)
    )
    if cached is not None:
        return cached
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
        seed = await seeder.discover(start_url)
    accepted, rejected, truncated, _stats = plan_targets(seed, start_url, scope, None)
    probe = DocProbe(
        start_url=start_url,
        source=seed.source,
        total_discovered=len(seed.urls),
        accepted=len(accepted),
        rejected=len(rejected),
        truncated=bool(truncated or seed.truncated),
        failed=bool(seed.failed),
        cross_site=len(seed.cross_site),
        prefix_groups=_prefix_groups(accepted),
        samples=list(accepted[: max(0, int(limit))]),
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
) -> DocProbe:
    import asyncio

    return asyncio.run(
        probe_docs_async(
            conn, settings, start_url, scope=scope, sample_limit=sample_limit
        )
    )
