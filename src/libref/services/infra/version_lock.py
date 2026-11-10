"""Per-version mutual exclusion shared by doc, code and retention writers.

The database row is the cross-process authority; in-process locks only
short-circuit local contention. Async writers share asyncio locks, the
synchronous purge path shares threading locks.
"""

from __future__ import annotations

import asyncio
import threading
import time


def version_key(ecosystem: str, library: str, version: str) -> str:
    """Identity of one library version across ecosystems."""
    return f"{ecosystem}\0{library}\0{version}"


def ensure_version_locks(conn) -> None:
    """Create the lock table when absent; idempotent."""
    from libref.db.schema import GOVERNANCE_SQL

    conn.executescript(GOVERNANCE_SQL)
    conn.commit()


_mem_locks: dict[str, asyncio.Lock] = {}
_mem_guard = threading.Lock()

_sync_locks: dict[str, threading.Lock] = {}
_sync_guard = threading.Lock()


def _mem_lock(key: str) -> asyncio.Lock:
    with _mem_guard:
        lock = _mem_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _mem_locks[key] = lock
        return lock


def _sync_lock(key: str) -> threading.Lock:
    with _sync_guard:
        lock = _sync_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _sync_locks[key] = lock
        return lock


class VersionGuard:
    """Async guard for one version; stale owners are stolen after the timeout."""

    def __init__(self, conn, key: str, stale_after: float = 600.0) -> None:
        self._conn = conn
        self._key = key
        self._stale_after = stale_after
        self._owner = f"{__import__('os').getpid()}-{time.time_ns()}"
        self._mem = _mem_lock(key)
        self._beat_task = None

    async def __aenter__(self):
        await self._mem.acquire()
        try:
            await self._claim()
        except BaseException:
            self._mem.release()
            raise
        self._beat_task = asyncio.get_running_loop().create_task(self._beat_loop())
        return self

    async def __aexit__(self, *exc):
        try:
            if self._beat_task is not None:
                self._beat_task.cancel()
        finally:
            try:
                from libref.db.conn import write_lock as _guard

                with _guard(self._conn):
                    self._conn.execute(
                        "DELETE FROM version_locks WHERE version_key=? AND owner=?",
                        (self._key, self._owner),
                    )
                    self._conn.commit()
            except Exception:  # noqa: BLE001, S110 — best-effort lock cleanup; release follows regardless
                pass
            self._mem.release()
        return False

    async def _claim(self) -> None:
        import sqlite3 as _sqlite3

        from libref.db.conn import write_lock as _guard

        ensure_version_locks(self._conn)
        while True:
            now = time.time()
            try:
                with _guard(self._conn):
                    self._conn.execute(
                        "INSERT INTO version_locks(version_key, owner, heartbeat_at) VALUES(?,?,?)",
                        (self._key, self._owner, now),
                    )
                    self._conn.commit()
                return
            except _sqlite3.IntegrityError:
                try:
                    self._conn.rollback()
                except Exception:  # noqa: BLE001, S110 — nothing left to roll back
                    pass
            row = self._conn.execute(
                "SELECT owner, heartbeat_at FROM version_locks WHERE version_key=?",
                (self._key,),
            ).fetchone()
            if row is None:
                continue
            owner, heartbeat = row[0], float(row[1] or 0)
            if owner == self._owner:
                return
            if now - heartbeat > self._stale_after:
                with _guard(self._conn):
                    cur = self._conn.execute(
                        "UPDATE version_locks SET owner=?, heartbeat_at=? WHERE version_key=? AND heartbeat_at=?",
                        (self._owner, now, self._key, heartbeat),
                    )
                    self._conn.commit()
                if cur.rowcount > 0:
                    return
            await asyncio.sleep(0.2)

    async def _beat_loop(self) -> None:
        from libref.db.conn import write_lock as _guard

        try:
            while True:
                await asyncio.sleep(max(1.0, self._stale_after / 6.0))
                try:
                    with _guard(self._conn):
                        self._conn.execute(
                            "UPDATE version_locks SET heartbeat_at=? WHERE version_key=? AND owner=?",
                            (time.time(), self._key, self._owner),
                        )
                        self._conn.commit()
                except Exception:  # noqa: BLE001, S112 — heartbeat failure retries on the next tick
                    continue
        except asyncio.CancelledError:
            return


class VersionGuardSync:
    """Synchronous guard for short operations; no heartbeat, stale owners are stolen."""

    def __init__(self, conn, key: str, stale_after: float = 600.0) -> None:
        self._conn = conn
        self._key = key
        self._stale_after = stale_after
        self._owner = f"{__import__('os').getpid()}-{time.time_ns()}"
        self._mem = _sync_lock(key)

    def __enter__(self):
        self._mem.acquire()
        try:
            self._claim()
        except BaseException:
            self._mem.release()
            raise
        return self

    def __exit__(self, *exc):
        try:
            try:
                from libref.db.conn import write_lock as _guard

                with _guard(self._conn):
                    self._conn.execute(
                        "DELETE FROM version_locks WHERE version_key=? AND owner=?",
                        (self._key, self._owner),
                    )
                    self._conn.commit()
            except Exception:  # noqa: BLE001, S110 — best-effort lock cleanup; release follows regardless
                pass
        finally:
            self._mem.release()
        return False

    def _claim(self) -> None:
        import sqlite3 as _sqlite3

        from libref.db.conn import write_lock as _guard

        ensure_version_locks(self._conn)
        while True:
            now = time.time()
            try:
                with _guard(self._conn):
                    self._conn.execute(
                        "INSERT INTO version_locks(version_key, owner, heartbeat_at) VALUES(?,?,?)",
                        (self._key, self._owner, now),
                    )
                    self._conn.commit()
                return
            except _sqlite3.IntegrityError:
                try:
                    self._conn.rollback()
                except Exception:  # noqa: BLE001, S110 — nothing left to roll back
                    pass
            row = self._conn.execute(
                "SELECT owner, heartbeat_at FROM version_locks WHERE version_key=?",
                (self._key,),
            ).fetchone()
            if row is None:
                continue
            owner, heartbeat = row[0], float(row[1] or 0)
            if owner == self._owner:
                return
            if now - heartbeat > self._stale_after:
                with _guard(self._conn):
                    cur = self._conn.execute(
                        "UPDATE version_locks SET owner=?, heartbeat_at=? WHERE version_key=? AND heartbeat_at=?",
                        (self._owner, now, self._key, heartbeat),
                    )
                    self._conn.commit()
                if cur.rowcount > 0:
                    return
            time.sleep(0.2)


def version_lock(conn, ecosystem: str, library: str, version: str):
    """Async guard for one version."""
    return VersionGuard(conn, version_key(ecosystem, library, version))


def version_lock_sync(conn, ecosystem: str, library: str, version: str):
    """Synchronous guard for one version."""
    return VersionGuardSync(conn, version_key(ecosystem, library, version))
