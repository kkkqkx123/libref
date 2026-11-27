"""Process-local adjacency snapshots derived from graph fact tables.

A snapshot is a rebuildable projection: SQLite and its fact tables remain the
only source of truth. The cache never answers queries itself; it only supplies
immutable adjacency records to ``store/graph.py``. A missing watermark, a
failed build, or an over-capacity snapshot always falls back to SQL.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CacheKey:
    """Identity of one adjacency snapshot; query-only bounds are excluded."""

    backend: str
    identity: str
    tenant: str | None
    version: int
    profiles: tuple[int, ...]
    profile_name: str
    include_ambiguous: bool
    pattern_limit: int
    watermark: str


@dataclass(slots=True)
class _Entry:
    snapshot: Any
    edge_count: int
    created: float
    used: float
    refs: int


class _Lease:
    """One active snapshot reader; idle entries with readers are never evicted."""

    __slots__ = ("_cache", "_key", "_released", "snapshot")

    def __init__(
        self, cache: GraphAdjacencyCache, key: CacheKey, snapshot: Any
    ) -> None:
        self._cache = cache
        self._key = key
        self.snapshot = snapshot
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._cache.release(self._key)

    def __del__(self) -> None:
        with suppress(Exception):
            self.release()


class GraphAdjacencyCache:
    """Bounded, lease-tracked registry of adjacency snapshots."""

    def __init__(self, max_edges: int, ttl_seconds: float) -> None:
        self.max_edges = max(1, int(max_edges))
        self.ttl = max(0.0, float(ttl_seconds))
        self._entries: dict[CacheKey, _Entry] = {}
        self._edge_count = 0
        self._lock = threading.RLock()
        self._stats = {
            "builds": 0,
            "hits": 0,
            "fallbacks": 0,
            "evictions": 0,
            "table_scans": 0,
        }

    def acquire(self, key: CacheKey, build: Callable[[], Any]) -> _Lease | None:
        """Return a leased snapshot, or ``None`` when SQL should be used."""
        with self._lock:
            now = time.monotonic()
            self._evict(now)
            entry = self._entries.get(key)
            if entry is not None:
                entry.used = now
                entry.refs += 1
                self._stats["hits"] += 1
                logger.debug(
                    "graph cache hit edges=%s version=%s profile=%s",
                    entry.edge_count,
                    key.version,
                    key.profile_name,
                )
                return _Lease(self, key, entry.snapshot)

            started = time.monotonic()
            try:
                snapshot = build()
            except Exception as exc:  # noqa: BLE001 -- build failure is a SQL fallback
                self._stats["fallbacks"] += 1
                logger.debug(
                    "graph cache build failed reason=%s error=%s",
                    type(exc).__name__,
                    exc,
                )
                return None
            if snapshot is None:
                self._stats["fallbacks"] += 1
                return None
            edge_count = int(getattr(snapshot, "edge_count", 0))
            if edge_count > self.max_edges:
                self._stats["fallbacks"] += 1
                logger.debug(
                    "graph cache capacity fallback edges=%s limit=%s",
                    edge_count,
                    self.max_edges,
                )
                return None
            while self._edge_count + edge_count > self.max_edges:
                victim = self._idle_victim(now, exclude=None)
                if victim is None:
                    self._stats["fallbacks"] += 1
                    logger.debug("graph cache capacity fallback: no idle snapshot")
                    return None
                self._drop(victim)

            self._entries[key] = _Entry(
                snapshot=snapshot,
                edge_count=edge_count,
                created=now,
                used=now,
                refs=1,
            )
            self._edge_count += edge_count
            self._stats["builds"] += 1
            scans = int(getattr(snapshot, "table_scans", 0))
            self._stats["table_scans"] += scans
            logger.debug(
                "graph cache build edges=%s scans=%s version=%s profile=%s"
                " elapsed=%.4fs",
                edge_count,
                scans,
                key.version,
                key.profile_name,
                time.monotonic() - started,
            )
            return _Lease(self, key, snapshot)

    def release(self, key: CacheKey) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.refs > 0:
                entry.refs -= 1
                entry.used = time.monotonic()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._edge_count = 0

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def _evict(self, now: float) -> None:
        while True:
            victim = self._idle_victim(now, exclude=None)
            if victim is None:
                return
            stale = now - self._entries[victim].created >= self.ttl
            idle = now - self._entries[victim].used >= self.ttl
            if not (stale or idle):
                return
            self._drop(victim)

    def _idle_victim(self, now: float, *, exclude: CacheKey | None) -> CacheKey | None:
        candidates = [
            key
            for key, entry in self._entries.items()
            if entry.refs == 0 and key != exclude
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda key: self._entries[key].used)

    def _drop(self, key: CacheKey) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        self._edge_count -= entry.edge_count
        self._stats["evictions"] += 1


_CACHES: dict[tuple[int, float], GraphAdjacencyCache] = {}
_CACHES_LOCK = threading.Lock()


def get_graph_adjacency_cache(
    max_edges: int, ttl_seconds: float
) -> GraphAdjacencyCache:
    """One shared cache per capacity/TTL pair; no background threads."""
    config = (max(1, int(max_edges)), max(0.0, float(ttl_seconds)))
    with _CACHES_LOCK:
        cache = _CACHES.get(config)
        if cache is None:
            cache = GraphAdjacencyCache(*config)
            _CACHES[config] = cache
        return cache


def clear_graph_adjacency_caches() -> None:
    """Drop all process-local snapshots, mainly for isolated tests."""
    with _CACHES_LOCK:
        for cache in _CACHES.values():
            cache.clear()


def graph_cache_context(conn: Any) -> tuple[str, str, str] | None:
    """Return backend identity and fact watermark, or ``None`` to disable caching."""
    raw = getattr(conn, "raw", conn)
    changes = getattr(raw, "total_changes", None)
    execute = getattr(raw, "execute", None)
    if changes is None or not callable(execute):
        return None
    try:
        data_version = int(execute("PRAGMA data_version").fetchone()[0])
        schema_version = int(execute("PRAGMA schema_version").fetchone()[0])
        files = execute("PRAGMA database_list").fetchall()
    except Exception as exc:  # noqa: BLE001 -- unsupported PRAGMA disables caching
        logger.debug("graph cache watermark unavailable reason=%s", type(exc).__name__)
        return None
    database = str(getattr(raw, "database", "") or "")
    for row in files:
        if str(row[1]) == "main" and row[2]:
            database = str(row[2])
            break
    if database and database != ":memory:" and not database.startswith("file:"):
        identity = os.path.abspath(database)
    else:
        identity = f"sqlite-memory:{id(raw)}"
    return (
        "sqlite",
        identity,
        f"{data_version}:{schema_version}:{int(changes)}",
    )
