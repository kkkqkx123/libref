"""HTTP fetching: httpx + etag/last_modified incrementals + per-host rate-limited backoff.

Follows the blueprint docs-mcp-server fetch layer's incremental semantics
(304 → short-circuit to the unchanged outcome) and crawl4ai's per-host backoff
idea. All requests go through the injected AsyncClient.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx

_BACKOFF_CAP = 8.0
_DELAY_CAP = 30.0
_RETRYABLE_STATUS = (408, 429)


@dataclass
class FetchOutcome:
    url: str
    status: str  # ok / unchanged / absent / failed (empty and skipped are decided by the orchestration layer)
    content: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None
    error: str | None = None


class HttpFetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_retries: int = 3,
        default_delay: float = 1.0,
    ) -> None:
        self._client = client
        self._max_retries = max_retries
        self._default_delay = default_delay
        self._last_request_at: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._penalty_until: dict[str, float] = {}
        self._penalty_level: dict[str, int] = {}

    async def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        crawl_delay: float | None = None,
        cache_mode: Literal["enabled", "bypass"] = "enabled",
    ) -> FetchOutcome:
        headers = {"Accept": "text/html, text/plain, text/markdown, */*"}
        if cache_mode == "enabled":
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified

        delay = crawl_delay if crawl_delay is not None else self._default_delay
        delay = min(max(delay, 0.0), _DELAY_CAP)
        error: str | None = None
        for attempt in range(self._max_retries + 1):
            await self._throttle(url, delay)
            try:
                resp = await self._client.get(url, headers=headers)
            except (httpx.HTTPError, OSError, TimeoutError) as exc:
                error = f"{type(exc).__name__}: {exc}"
                await self._mark(url)
                await asyncio.sleep(min(0.5 * (2**attempt), _BACKOFF_CAP))
                continue
            await self._mark(url)
            if resp.status_code == 304:
                self._relieve(url)
                return FetchOutcome(url=url, status="unchanged")
            if resp.status_code == 404 or resp.status_code == 410:
                self._relieve(url)
                return FetchOutcome(
                    url=url, status="absent", error=f"HTTP {resp.status_code}"
                )
            if resp.status_code >= 500 or resp.status_code in _RETRYABLE_STATUS:
                error = f"HTTP {resp.status_code}"
                self._penalize(url)
                await asyncio.sleep(min(0.5 * (2**attempt), _BACKOFF_CAP))
                continue
            if resp.status_code >= 400:
                self._relieve(url)
                return FetchOutcome(
                    url=url, status="failed", error=f"HTTP {resp.status_code}"
                )
            self._relieve(url)
            return FetchOutcome(
                url=url,
                status="ok",
                content=resp.text,
                etag=resp.headers.get("etag"),
                last_modified=resp.headers.get("last-modified"),
                content_type=resp.headers.get("content-type"),
            )
        return FetchOutcome(url=url, status="failed", error=error)

    def penalized_hosts(self) -> list[str]:
        """Hosts currently under rate-limit penalty; observability only."""
        now = time.monotonic()
        return [h for h, until in self._penalty_until.items() if until > now]

    def _penalize(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        level = self._penalty_level.get(host, 0) + 1
        self._penalty_level[host] = level
        self._penalty_until[host] = time.monotonic() + min(
            0.5 * (2**level), _BACKOFF_CAP
        )

    def _relieve(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        self._penalty_level.pop(host, None)
        self._penalty_until.pop(host, None)

    async def _throttle(self, url: str, delay: float) -> None:
        host = urlsplit(url).netloc.lower()
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = 0.0
            last = self._last_request_at.get(host)
            if last is not None:
                wait = max(wait, delay - (time.monotonic() - last))
            penalty = self._penalty_until.get(host)
            if penalty is not None:
                wait = max(wait, penalty - time.monotonic())
            if wait > 0:
                await asyncio.sleep(wait)

    async def _mark(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            self._last_request_at[host] = time.monotonic()
