"""robots 合规检查 + SQLite 缓存（TTL 7 天）。

蓝本 crawl4ai utils.py::RobotsParser（SQLite 缓存 + MD5 变更检测）。
定制点：解析用 stdlib urllib.robotparser；缓存表由 ensure_robots_cache 建表，
属合规运维缓存而非业务 schema，不入版本化迁移。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
import urllib.robotparser
from urllib.parse import urlsplit

_ROBOTS_CACHE_SQL = """
CREATE TABLE IF NOT EXISTS robots_cache(
  host TEXT PRIMARY KEY,
  body TEXT NOT NULL,
  body_md5 TEXT NOT NULL,
  fetched_at REAL NOT NULL
);
"""


def ensure_robots_cache(conn: sqlite3.Connection) -> None:
    conn.execute(_ROBOTS_CACHE_SQL)
    conn.commit()


class RobotsChecker:
    """per-host robots 检查；结果缓存于 SQLite，TTL 过期后重取。

    robots 不可达（404 / 网络错误）视为全允许（crawl4ai 同语义）。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        client,  # httpx.AsyncClient
        ttl_days: float = 7.0,
        user_agent: str = "libref-crawler/0.1",
    ) -> None:
        self._conn = conn
        self._client = client
        self._ttl = ttl_days * 86400
        self._ua = user_agent
        ensure_robots_cache(conn)

    async def can_fetch(self, url: str) -> bool:
        rp = await self._parser_for(url)
        if rp is None:
            return True
        return rp.can_fetch(self._ua, url)

    async def crawl_delay(self, url: str) -> float | None:
        rp = await self._parser_for(url)
        if rp is None:
            return None
        delay = rp.crawl_delay(self._ua)
        return float(delay) if delay is not None else None

    # ── 内部 ──

    def _cache_key(self, url: str) -> str:
        parts = urlsplit(url)
        scheme = (parts.scheme or "https").lower()
        return f"{scheme}://{parts.netloc.lower()}"

    async def _parser_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        key = self._cache_key(url)
        body = self._cached_body(key)
        if body is None:
            body = await self._fetch_body(url)
            if body is None:
                return None  # robots 不可达 → 全允许
            self._store(key, body)
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(body.splitlines())
        return rp

    def _cached_body(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT body, body_md5, fetched_at FROM robots_cache WHERE host=?", (key,)
        ).fetchone()
        if row is None:
            return None
        body, md5, fetched_at = row
        if time.time() - fetched_at > self._ttl:
            return None
        if hashlib.md5(body.encode()).hexdigest() != md5:
            return None
        return body

    def _store(self, key: str, body: str) -> None:
        self._conn.execute(
            "INSERT INTO robots_cache(host, body, body_md5, fetched_at) VALUES(?,?,?,?) "
            "ON CONFLICT(host) DO UPDATE SET body=excluded.body, body_md5=excluded.body_md5, "
            "fetched_at=excluded.fetched_at",
            (key, body, hashlib.md5(body.encode()).hexdigest(), time.time()),
        )
        self._conn.commit()

    async def _fetch_body(self, url: str) -> str | None:
        import httpx

        parts = urlsplit(url)
        scheme = (parts.scheme or "https").lower()
        target = f"{scheme}://{parts.netloc}/robots.txt"
        try:
            resp = await self._client.get(target)
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400:
            return None
        return resp.text
