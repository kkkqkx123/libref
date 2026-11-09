"""robots compliance checking + SQLite cache (7-day TTL).

Follows the blueprint crawl4ai utils.py::RobotsParser (SQLite cache + MD5
change detection). Customization points: parsing via stdlib
urllib.robotparser; the cache table is created via versioned migrations and
idempotently ensured on demand at runtime.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
import urllib.robotparser
from urllib.parse import urlsplit

def ensure_robots_cache(conn: sqlite3.Connection) -> None:
    from libref.db.schema import GOVERNANCE_SQL

    conn.executescript(GOVERNANCE_SQL)
    conn.commit()


class RobotsChecker:
    """Per-host robots checking; results cached in SQLite and re-fetched after TTL expiry.

    An unreachable robots file (404 / network error) means fully allow
    (same semantics as crawl4ai).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        client,  # httpx.AsyncClient
        ttl_days: float | None = None,
        user_agent: str | None = None,
    ) -> None:
        from libref.config import LibrefSettings

        fields = LibrefSettings.model_fields
        self._conn = conn
        self._client = client
        resolved_ttl = fields["robots_cache_ttl_days"].default if ttl_days is None else ttl_days
        resolved_ua = fields["user_agent"].default if user_agent is None else user_agent
        self._ttl = resolved_ttl * 86400
        self._ua = resolved_ua
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

    async def sitemap_urls(self, start_url: str) -> list[str]:
        """Sitemap URLs declared in robots, deduplicated in order of appearance."""
        body = await self.raw_body(start_url)
        if not body:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" not in stripped:
                continue
            name, _, value = stripped.partition(":")
            if name.strip().lower() != "sitemap":
                continue
            loc = value.strip().strip("<>\"'")
            if loc and loc not in seen:
                seen.add(loc)
                out.append(loc)
        return out

    async def raw_body(self, url: str) -> str | None:
        """Fetch the robots text (returned directly on cache hit); return None when unreachable."""
        key = self._cache_key(url)
        body = self._cached_body(key)
        if body is not None:
            return body
        body = await self._fetch_body(url)
        if body is None:
            return None
        self._store(key, body)
        return body

    # ── internal ──

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
                return None  # robots unreachable → fully allow
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
