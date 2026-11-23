"""Job scheduling for document feature derivation."""

from __future__ import annotations

import asyncio
from typing import Any

from libref.services.docs.feature_refresh import (
    build_doc_feature_runner,
    refresh_doc_features,
)
from libref.services.infra.jobs import DOC_FEATURE_KIND, Job, JobManager

__all__ = [
    "DOC_FEATURE_KIND",
    "build_doc_feature_runner",
    "doc_feature_job_params",
    "refresh_doc_features",
    "run_doc_feature_job_sync",
    "start_doc_feature_job",
]


def doc_feature_job_params(
    library: str,
    version: str,
    *,
    ecosystem: str | None = None,
    profile: str | None = None,
    scope: str = "all",
    tenant_id: str = "default",
) -> dict[str, Any]:
    """Build the replayable parameter snapshot for one derivation job."""
    return {
        "library": library,
        "version": version,
        "ecosystem": ecosystem,
        "profile": profile,
        "scope": scope,
        "tenant_id": tenant_id,
    }


def start_doc_feature_job(
    mgr: JobManager,
    library: str,
    version: str,
    *,
    ecosystem: str | None = None,
    profile: str | None = None,
    scope: str = "all",
    tenant_id: str = "default",
    principal_id: int | None = None,
) -> int:
    """Submit a derivation job and return its id."""
    return mgr.submit(
        DOC_FEATURE_KIND,
        doc_feature_job_params(
            library,
            version,
            ecosystem=ecosystem,
            profile=profile,
            scope=scope,
            tenant_id=tenant_id,
        ),
        tenant_id=tenant_id,
        principal_id=principal_id,
    )


def run_doc_feature_job_sync(
    mgr: JobManager,
    settings,
    conn,
    job_id: int,
    *,
    timeout: float = 300.0,
) -> Job:
    """Drive one derivation job synchronously for the command-line async toggle."""

    async def _drive() -> Job:
        mgr.start_worker(build_doc_feature_runner(settings, conn))
        try:
            return await mgr.wait_for(job_id, timeout=timeout)
        finally:
            mgr.stop_worker()

    return asyncio.run(_drive())
