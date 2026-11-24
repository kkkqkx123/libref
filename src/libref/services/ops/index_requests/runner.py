"""Worker runner for index request jobs."""

from __future__ import annotations

import asyncio
from typing import Any

from libref.services.infra.jobs import Job, JobManager
from libref.services.ops.index_requests.models import IndexRequestError, _as_int


def build_index_request_runner(
    settings: Any, conn: Any, inner: Any, jobs: JobManager | None = None
):
    """Wrap a job runner so every request-backed job writes its terminal state."""
    from libref.services.ops.index_requests import IndexRequestService

    svc = IndexRequestService(conn, settings, jobs=jobs)

    async def runner(job: Job):
        rid = (
            _as_int(job.params.get("index_request_id"))
            if isinstance(job.params, dict)
            else None
        )
        if rid is None:
            return await inner(job)
        if jobs is not None and jobs.is_cancel_requested(job.id):
            svc.finish(rid, status="cancelled", error="cancelled")
            raise IndexRequestError(
                "index request cancelled", code="cancelled", status=409
            )
        if not svc.start(rid):
            current = svc._row(rid)
            raise IndexRequestError(
                "index request cancelled"
                if current is not None and current["status"] == "cancelled"
                else "index request is not queued",
                code="cancelled"
                if current is not None and current["status"] == "cancelled"
                else "conflict",
                status=409,
            )
        try:
            produced = inner(job)
            if asyncio.iscoroutine(produced):
                produced = await produced
        except BaseException as exc:
            svc.finish(rid, status="failed", error=f"{type(exc).__name__}: {exc}")
            raise
        current = svc._row(rid)
        if current is not None and current["status"] == "cancelled":
            raise IndexRequestError(
                "index request cancelled", code="cancelled", status=409
            )
        svc.finish(rid, status="done")
        return produced

    return runner
