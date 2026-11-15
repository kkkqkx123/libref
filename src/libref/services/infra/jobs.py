"""Database-claimed job queue with persistence plus heartbeat recovery.

Submissions only write the jobs table. Workers atomically claim pending rows
with a single conditional update, so multiple processes never double-run one
job. Governance tables are created by versioned migration and ensured at
runtime for fresh test databases.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_jobs_table(conn: sqlite3.Connection) -> None:
    """Create governance tables and backfill job columns for legacy databases."""
    from libref.db.schema import AUTH_TABLES_SQL, GOVERNANCE_SQL

    conn.executescript(GOVERNANCE_SQL)
    conn.executescript(AUTH_TABLES_SQL)
    cols = _table_columns(conn, "jobs")
    if "result" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN result JSON NOT NULL DEFAULT '{}'")
        cols.add("result")
    if "finished_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN finished_at TEXT")
        cols.add("finished_at")
    if "tenant_id" not in cols:
        conn.execute(
            "ALTER TABLE jobs ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'"
        )
    if "principal_id" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN principal_id INTEGER")
    if "owner" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN owner TEXT")
    if "cancel_requested" not in cols:
        conn.execute(
            "ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
        )
    if "attempts" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    conn.commit()


_STALE_AFTER = 600.0


def summarize_result(value: object) -> object:
    """Summarize a job payload for storage; long lists are truncated."""
    if isinstance(value, dict):
        return {str(k): summarize_result(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [summarize_result(v) for v in value]
        if len(items) > 100:
            return {"items": items[:100], "truncated": len(items) - 100}
        return items
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


@dataclass
class Job:
    id: int
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    error: str | None = None
    result: Any = field(default_factory=dict)
    finished_at: str | None = None
    tenant_id: str = "default"
    principal_id: int | None = None
    owner: str | None = None


class JobManager:
    """Database-claimed queue; submit writes, workers claim atomically."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        stale_after: float = _STALE_AFTER,
        instance_id: str = "libref-1",
    ) -> None:
        self._conn = conn
        self._stale_after = stale_after
        self._instance_id = instance_id
        self._worker: asyncio.Task[None] | None = None
        self._stop = False
        ensure_jobs_table(conn)

    def _owner(self) -> str:
        return f"{self._instance_id}-{os.getpid()}"

    def submit(
        self,
        kind: str,
        params: dict[str, Any] | None = None,
        *,
        tenant_id: str = "default",
        principal_id: int | None = None,
    ) -> int:
        """Write a pending row; workers discover it by polling."""
        from libref.db.conn import write_lock

        with write_lock(self._conn):
            cur = self._conn.execute(
                "INSERT INTO jobs(kind, params, tenant_id, principal_id)"
                " VALUES(?, ?, ?, ?)",
                (
                    kind,
                    json.dumps(params or {}, ensure_ascii=False),
                    tenant_id or "default",
                    principal_id,
                ),
            )
            self._conn.commit()
        assert cur.lastrowid is not None
        job_id = int(cur.lastrowid)
        logger.debug("job submitted id=%d kind=%s", job_id, kind)
        return job_id

    def claim_next(self) -> Job | None:
        """Atomically claim one pending row; only the winner gets rowcount 1."""
        from libref.db.conn import write_lock

        owner = self._owner()
        with write_lock(self._conn):
            row = self._conn.execute(
                "SELECT id FROM jobs WHERE status='pending'"
                " AND cancel_requested=0 ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            job_id = int(row[0])
            cur = self._conn.execute(
                "UPDATE jobs SET status='running', owner=?, heartbeat_at=?,"
                " attempts=attempts+1 WHERE id=? AND status='pending'",
                (owner, time.time(), job_id),
            )
            self._conn.commit()
            if cur.rowcount != 1:
                return None
        return self.get(job_id)

    def get(self, job_id: int) -> Job | None:
        cols = _table_columns(self._conn, "jobs")
        has_tenant = "tenant_id" in cols
        if has_tenant:
            row = self._conn.execute(
                "SELECT id, kind, params, status, error, result, finished_at,"
                " tenant_id, principal_id, owner"
                " FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT id, kind, params, status, error, result, finished_at"
                " FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            result = json.loads(row[5]) if row[5] else {}
        except ValueError:
            result = {}
        return Job(
            id=row[0],
            kind=row[1],
            params=json.loads(row[2]),
            status=row[3],
            error=row[4],
            result=result,
            finished_at=row[6],
            tenant_id=str(row[7]) if has_tenant else "default",
            principal_id=row[8] if has_tenant else None,
            owner=row[9] if has_tenant else None,
        )

    def list_recent(
        self, limit: int = 20, *, tenant_id: str | None = None
    ) -> list[Job]:
        cols = _table_columns(self._conn, "jobs")
        has_tenant = "tenant_id" in cols
        scoped = has_tenant and tenant_id not in (None, "*")
        if has_tenant and scoped:
            rows = self._conn.execute(
                "SELECT id, kind, params, status, error, result, finished_at,"
                " tenant_id, principal_id, owner"
                " FROM jobs WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
                (
                    tenant_id,
                    limit,
                ),
            ).fetchall()
        elif has_tenant:
            rows = self._conn.execute(
                "SELECT id, kind, params, status, error, result, finished_at,"
                " tenant_id, principal_id, owner"
                " FROM jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, kind, params, status, error, result, finished_at"
                " FROM jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out: list[Job] = []
        for row in rows:
            try:
                result = json.loads(row[5]) if row[5] else {}
            except ValueError:
                result = {}
            out.append(
                Job(
                    id=row[0],
                    kind=row[1],
                    params=json.loads(row[2]),
                    status=row[3],
                    error=row[4],
                    result=result,
                    finished_at=row[6],
                    tenant_id=str(row[7]) if has_tenant else "default",
                    principal_id=row[8] if has_tenant else None,
                    owner=row[9] if has_tenant else None,
                )
            )
        return out

    def show(
        self,
        job_id: int | None = None,
        limit: int = 20,
        *,
        tenant_id: str | None = None,
        principal_id: int | None = None,
    ) -> dict[str, Any]:
        """List recent jobs or one detail, filtered by tenant when given."""
        if job_id is None:
            jobs = self.list_recent(limit=limit, tenant_id=tenant_id)
            return {
                "jobs": [
                    {
                        "id": j.id,
                        "kind": j.kind,
                        "status": j.status,
                        "finished_at": j.finished_at,
                        "error": j.error,
                    }
                    for j in jobs
                ]
            }
        job = self.get(job_id)
        if job is None:
            return {"error": "not_found", "message": f"unknown job: {job_id}"}
        if tenant_id is not None and tenant_id != "*" and job.tenant_id != tenant_id:
            return {"error": "not_found", "message": f"unknown job: {job_id}"}
        return {
            "id": job.id,
            "kind": job.kind,
            "status": job.status,
            "error": job.error,
            "result": job.result,
            "finished_at": job.finished_at,
        }

    def cancel(self, job_id: int) -> None:
        """Request cancellation; pending rows fail fast, running rows observe the flag."""
        from libref.db.conn import write_lock

        with write_lock(self._conn):
            cur = self._conn.execute(
                "UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,)
            )
            self._conn.commit()
            if cur.rowcount == 0:
                raise ValueError(f"unknown job: {job_id}")
            row = self._conn.execute(
                "SELECT status FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is not None and row[0] == "pending":
                self._conn.execute(
                    "UPDATE jobs SET status='failed', error='cancelled',"
                    " finished_at=datetime('now') WHERE id=? AND status='pending'",
                    (job_id,),
                )
                self._conn.commit()

    def heartbeat(self, job_id: int) -> None:
        """Renew a running job heartbeat so recovery skips it."""
        self._update(job_id, status="running", heartbeat_at=time.time())

    def start_worker(self, runner, *, poll_interval: float = 0.5) -> None:
        """Poll pending rows with idle backoff; sync or async runners both work."""
        self._stop = False

        async def _loop() -> None:
            idle = 0
            while not self._stop:
                job = self.claim_next()
                if job is None:
                    idle += 1
                    backoff = min(2.0, poll_interval * (1.5 ** min(idle, 6)))
                    await asyncio.sleep(backoff)
                    continue
                idle = 0
                await self._execute(job, runner)

        self._worker = asyncio.get_running_loop().create_task(_loop())

    async def _execute(self, job: Job, runner) -> None:
        beat = asyncio.get_running_loop().create_task(self._beat_loop(job.id))
        try:
            row = self._conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id=?", (job.id,)
            ).fetchone()
            if row is not None and int(row[0] or 0) == 1:
                self._update(
                    job.id,
                    status="failed",
                    error="cancelled",
                    finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                )
                return
            produced = runner(job)
            if asyncio.iscoroutine(produced):
                produced = await produced
            payload = json.dumps(summarize_result(produced), ensure_ascii=False)
            self._update(
                job.id,
                status="done",
                heartbeat_at=time.time(),
                result=payload,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            )
            logger.info("job done id=%d kind=%s", job.id, job.kind)
        except BaseException as exc:  # noqa: BLE001
            self._update(
                job.id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            )
            logger.warning(
                "job failed id=%d kind=%s error=%s: %s",
                job.id,
                job.kind,
                type(exc).__name__,
                exc,
            )
        finally:
            beat.cancel()

    async def _beat_loop(self, job_id: int) -> None:
        try:
            interval = 5.0
            try:
                interval = max(1.0, float(self._stale_after) / 6.0)
            except TypeError, ValueError:
                pass
            while True:
                await asyncio.sleep(min(interval, 5.0))
                try:
                    job = self.get(job_id)
                except Exception:  # noqa: BLE001, S112 — transient read retries next tick
                    continue
                if job is None or job.status != "running":
                    return
                self._update(job_id, heartbeat_at=time.time())
        except asyncio.CancelledError:
            return

    def stop_worker(self) -> None:
        self._stop = True
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None

    async def wait_for(
        self, job_id: int, *, timeout: float = 300.0, poll: float = 0.1
    ) -> Job:
        """Wait until one job settles; raises TimeoutError or KeyError."""
        deadline = time.monotonic() + timeout
        while True:
            job = self.get(job_id)
            if job is None:
                raise KeyError(f"unknown job: {job_id}")
            if job.status in ("done", "failed"):
                return job
            if time.monotonic() >= deadline:
                raise TimeoutError(f"job {job_id} not finished within {timeout}s")
            await asyncio.sleep(poll)

    def recover(self, *, requeue: bool = False) -> list[int]:
        """Mark stale running rows failed, or requeue them when asked."""
        stale_ids = [
            r[0]
            for r in self._conn.execute(
                "SELECT id FROM jobs WHERE status='running' AND ("
                "heartbeat_at IS NULL OR ? - heartbeat_at > ?) ORDER BY id",
                (time.time(), self._stale_after),
            )
        ]
        for job_id in stale_ids:
            if requeue:
                self._update(
                    job_id,
                    status="pending",
                    error="stale: heartbeat timeout",
                    heartbeat_at=None,
                    finished_at=None,
                    owner=None,
                )
            else:
                self._update(
                    job_id,
                    status="failed",
                    error="stale: heartbeat timeout",
                    finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                )
        if stale_ids:
            logger.warning("job stale recovered ids=%s requeue=%s", stale_ids, requeue)
        return stale_ids

    def _update(self, job_id: int, **fields: Any) -> None:
        from libref.db.conn import write_lock

        sets = ", ".join(f"{k}=?" for k in fields)
        with write_lock(self._conn):
            self._conn.execute(
                f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id)
            )
            self._conn.commit()
