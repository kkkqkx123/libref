"""Approval-gated index requests: service facade and public contracts."""

from __future__ import annotations

from libref.services.infra.jobs import JobManager
from libref.services.ops.index_requests.intake import Intake
from libref.services.ops.index_requests.lifecycle import Lifecycle
from libref.services.ops.index_requests.models import (
    ACTIVE_STATUSES,
    INDEX_JOB_KINDS,
    TERMINAL_STATUSES,
    IndexRequestError,
    IndexRequestItem,
    QuotaDenied,
)
from libref.services.ops.index_requests.queries import Queries
from libref.services.ops.index_requests.runner import build_index_request_runner

__all__ = [
    "ACTIVE_STATUSES",
    "INDEX_JOB_KINDS",
    "TERMINAL_STATUSES",
    "IndexRequestError",
    "IndexRequestItem",
    "IndexRequestService",
    "QuotaDenied",
    "build_index_request_runner",
]


class IndexRequestService(Intake, Lifecycle, Queries):
    """Normalize, de-duplicate, approve, execute, and read index requests."""

    TERMINAL_STATUSES = TERMINAL_STATUSES
    ACTIVE_STATUSES = ACTIVE_STATUSES

    def __init__(self, conn, settings, jobs: JobManager | None = None) -> None:
        self._conn = conn
        self._settings = settings
        self._jobs = jobs if jobs is not None else JobManager(conn)
