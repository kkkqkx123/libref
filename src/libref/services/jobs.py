"""In-process async job queue + write-through persistence + heartbeat recovery.

The in-memory queue accepts submissions and writes back to the job table; a worker consumes and executes, and jobs whose heartbeat was not renewed within the timeout are recovered as stale.
Governance tables are created via versioned migration and idempotently ensured at runtime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_jobs_table(conn: sqlite3.Connection) -> None:
    from libref.db.schema import GOVERNANCE_SQL

    conn.executescript(GOVERNANCE_SQL)
    cols = _table_columns(conn, "jobs")
    if "result" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN result JSON NOT NULL DEFAULT '{}'")
    if "finished_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN finished_at TEXT")
    conn.commit()


_STALE_AFTER = 600.0


def summarize_result(value: object) -> object:
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


class JobManager:
    """In-process queue; submit writes the jobs table, the worker consumes and writes back status, recover handles stale."""

    def __init__(
        self, conn: sqlite3.Connection, *, stale_after: float = _STALE_AFTER
    ) -> None:
        self._conn = conn
        self._stale_after = stale_after
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        ensure_jobs_table(conn)

    # ── submit / query ──

    def submit(self, kind: str, params: dict[str, Any] | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO jobs(kind, params) VALUES(?, ?)",
            (kind, json.dumps(params or {}, ensure_ascii=False)),
        )
        self._conn.commit()
        job_id = int(cur.lastrowid)
        self._queue.put_nowait(job_id)
        logger.debug("job submitted id=%d kind=%s", job_id, kind)
        return job_id

    def get(self, job_id: int) -> Job | None:
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
        )

    def list_recent(self, limit: int = 20) -> list[Job]:
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
                )
            )
        return out

    def show(self, job_id: int | None = None, limit: int = 20) -> dict[str, Any]:
        """Unified job view: list recent jobs when no id is given, detail otherwise."""
        if job_id is None:
            return {
                "jobs": [
                    {
                        "id": j.id,
                        "kind": j.kind,
                        "status": j.status,
                        "finished_at": j.finished_at,
                        "error": j.error,
                    }
                    for j in self.list_recent(limit=limit)
                ]
            }
        job = self.get(job_id)
        if job is None:
            return {"error": "not_found", "message": f"unknown job: {job_id}"}
        return {
            "id": job.id,
            "kind": job.kind,
            "status": job.status,
            "error": job.error,
            "result": job.result,
            "finished_at": job.finished_at,
        }

    # ── worker ──

    def heartbeat(self, job_id: int) -> None:
        """Renew the heartbeat of a long-running job; the scrape side calls back per page to avoid long jobs being misjudged as stale."""
        self._update(job_id, status="running", heartbeat_at=time.time())

    def start_worker(self, runner) -> None:
        """runner(job: Job) -> None: sync function or coroutine; any exception is caught wholesale as failed."""
        self._worker = asyncio.get_running_loop().create_task(self._run(runner))

    async def _run(self, runner) -> None:
        while True:
            job_id = await self._queue.get()
            job = self.get(job_id)
            if job is None:
                self._queue.task_done()
                continue
            self._update(job_id, status="running", heartbeat_at=time.time())
            beat = asyncio.get_running_loop().create_task(self._beat_loop(job_id))
            try:
                produced = runner(job)
                if asyncio.iscoroutine(produced):
                    produced = await produced
                payload = json.dumps(summarize_result(produced), ensure_ascii=False)
                self._update(
                    job_id,
                    status="done",
                    heartbeat_at=time.time(),
                    result=payload,
                    finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                )
                logger.info("job done id=%d kind=%s", job_id, job.kind)
            except BaseException as exc:  # noqa: BLE001
                self._update(
                    job_id,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                )
                logger.warning(
                    "job failed id=%d kind=%s error=%s: %s",
                    job_id,
                    job.kind,
                    type(exc).__name__,
                    exc,
                )
            finally:
                beat.cancel()
                self._queue.task_done()

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
                except Exception:  # noqa: BLE001, S112 — a transient read failure retries on the next tick
                    continue
                if job is None or job.status != "running":
                    return
                self._update(job_id, heartbeat_at=time.time())
        except asyncio.CancelledError:
            return

    def stop_worker(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None

    async def wait_for(
        self, job_id: int, *, timeout: float = 300.0, poll: float = 0.02
    ) -> Job:
        """Wait for the job to settle (done/failed); raises TimeoutError on timeout."""
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

    # ── heartbeat recovery ──

    def recover(self) -> list[int]:
        stale_ids = [
            r[0]
            for r in self._conn.execute(
                "SELECT id FROM jobs WHERE status='running' AND ("
                "heartbeat_at IS NULL OR ? - heartbeat_at > ?)",
                (time.time(), self._stale_after),
            )
        ]
        for job_id in stale_ids:
            self._update(
                job_id,
                status="failed",
                error="stale: heartbeat timeout",
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            )
        if stale_ids:
            logger.warning("job stale recovered ids=%s", stale_ids)
        return stale_ids

    # ── internal ──

    def _update(self, job_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(
            f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id)
        )
        self._conn.commit()
