"""HTTP 抓取：httpx + etag/last_modified 增量 + per-host 限速退避。

蓝本 docs-mcp-server 抓取层增量语义（304 → unchanged 结局短路）与
crawl4ai per-host 退避思路。所有请求经注入的 AsyncClient。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


@dataclass
class FetchOutcome:
    url: str
    status: str  # ok / unchanged / failed
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

    async def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        crawl_delay: float | None = None,
    ) -> FetchOutcome:
        headers = {"Accept": "text/html, text/plain, text/markdown, */*"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        delay = crawl_delay if crawl_delay is not None else self._default_delay
        error: str | None = None
        for attempt in range(self._max_retries + 1):
            await self._throttle(url, delay)
            try:
                resp = await self._client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            if resp.status_code == 304:
                return FetchOutcome(url=url, status="unchanged")
            if resp.status_code == 404 or resp.status_code == 410:
                return FetchOutcome(url=url, status="absent", error=f"HTTP {resp.status_code}")
            if resp.status_code >= 500:
                error = f"HTTP {resp.status_code}"
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            if resp.status_code >= 400:
                return FetchOutcome(url=url, status="failed", error=f"HTTP {resp.status_code}")
            return FetchOutcome(
                url=url,
                status="ok",
                content=resp.text,
                etag=resp.headers.get("etag"),
                last_modified=resp.headers.get("last-modified"),
                content_type=resp.headers.get("content-type"),
            )
        return FetchOutcome(url=url, status="failed", error=error)

    async def _throttle(self, url: str, delay: float) -> None:
        host = urlsplit(url).netloc
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._last_request_at.get(host)
            if last is not None:
                wait = delay - (time.monotonic() - last)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_request_at[host] = time.monotonic()
