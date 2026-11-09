"""进程内异步任务队列 + write-through 落库 + 心跳恢复。

内存队列提交任务并回写任务表，worker 消费执行，超时未续写按 stale 恢复。
jobs 表为运维表，由 ensure_jobs_table 建表，不入业务迁移。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

_JOBS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  params JSON NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending','running','done','failed')),
  error TEXT,
  heartbeat_at REAL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_STALE_AFTER = 600.0  # 心跳超时（秒），超过视为 stale


def ensure_jobs_table(conn: sqlite3.Connection) -> None:
    conn.execute(_JOBS_TABLE_SQL)
    conn.commit()


@dataclass
class Job:
    id: int
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    error: str | None = None


class JobManager:
    """进程内队列；submit 落 jobs 表，worker 消费并回写状态，recover 处理 stale。"""

    def __init__(self, conn: sqlite3.Connection, *, stale_after: float = _STALE_AFTER) -> None:
        self._conn = conn
        self._stale_after = stale_after
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        ensure_jobs_table(conn)

    # ── 提交 / 查询 ──

    def submit(self, kind: str, params: dict[str, Any] | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO jobs(kind, params) VALUES(?, ?)",
            (kind, json.dumps(params or {}, ensure_ascii=False)),
        )
        self._conn.commit()
        job_id = int(cur.lastrowid)
        self._queue.put_nowait(job_id)
        return job_id

    def get(self, job_id: int) -> Job | None:
        row = self._conn.execute(
            "SELECT id, kind, params, status, error FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return Job(id=row[0], kind=row[1], params=json.loads(row[2]),
                   status=row[3], error=row[4])

    # ── worker ──

    def heartbeat(self, job_id: int) -> None:
        """长任务续写心跳，抓取侧按页回调，避免长耗时被误判 stale。"""
        self._update(job_id, status="running", heartbeat_at=time.time())

    def start_worker(self, runner) -> None:
        """runner(job: Job) -> None：同步或协程均可；异常整体捕获为 failed。"""
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
                result = runner(job)
                if asyncio.iscoroutine(result):
                    await result
                self._update(job_id, status="done", heartbeat_at=time.time())
            except BaseException as exc:  # noqa: BLE001 —— 单任务失败不拖垮 worker
                self._update(job_id, status="failed", error=f"{type(exc).__name__}: {exc}")
            finally:
                beat.cancel()
                self._queue.task_done()

    async def _beat_loop(self, job_id: int) -> None:
        """后台续写心跳，间隔按超时阈值推导，任务结束即取消。"""
        try:
            interval = 5.0
            try:
                interval = max(1.0, float(self._stale_after) / 6.0)
            except (TypeError, ValueError):
                pass
            while True:
                await asyncio.sleep(min(interval, 5.0))
                try:
                    job = self.get(job_id)
                except Exception:  # noqa: BLE001 —— 心跳只求续写，不干扰主任务
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

    async def wait_for(self, job_id: int, *, timeout: float = 300.0,
                       poll: float = 0.02) -> Job:
        """等待任务落定（done/failed），超时抛 TimeoutError。"""
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

    # ── 心跳恢复 ──

    def recover(self) -> list[int]:
        """启动时调用：running 且心跳超时的任务标 failed(stale)。返回受影响 id。"""
        stale_ids = [
            r[0]
            for r in self._conn.execute(
                "SELECT id FROM jobs WHERE status='running' AND ("
                "heartbeat_at IS NULL OR ? - heartbeat_at > ?)",
                (time.time(), self._stale_after),
            )
        ]
        for job_id in stale_ids:
            self._update(job_id, status="failed", error="stale: heartbeat timeout")
        return stale_ids

    # ── 内部 ──

    def _update(self, job_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
        self._conn.commit()
