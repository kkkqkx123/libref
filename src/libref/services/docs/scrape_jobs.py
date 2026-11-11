"""Scrape job scheduling over the shared job table."""

from __future__ import annotations

import asyncio
from typing import Any

from libref.llm import EmbedFn
from libref.services.infra.jobs import Job, JobManager

SCRAPE_JOB_KIND = "scrape_docs"
"""Job kind name for scrape jobs in the job table."""


def scrape_job_params(
    library: str,
    version: str,
    start_url: str,
    *,
    max_pages: int | None = None,
    prefilter: bool = False,
    scope: dict[str, Any] | None = None,
    ecosystem: str = "custom",
    resolved_from: str | None = None,
    registry_confirmed: bool = True,
) -> dict[str, Any]:
    """Build scrape job params, including the resolution source for replayability."""
    return {
        "library": library,
        "version": version,
        "start_url": start_url,
        "max_pages": max_pages,
        "prefilter": prefilter,
        "scope": scope or {},
        "ecosystem": ecosystem,
        "resolved_from": resolved_from,
        "registry_confirmed": registry_confirmed,
    }


def start_scrape_job(
    mgr: JobManager,
    library: str,
    version: str,
    start_url: str,
    *,
    max_pages: int | None = None,
    prefilter: bool = False,
    scope: dict[str, Any] | None = None,
    ecosystem: str = "custom",
    resolved_from: str | None = None,
    registry_confirmed: bool = True,
) -> int:
    """Submit a scrape job to the queue and return its id; execution is done by the worker plus runner."""
    return mgr.submit(
        SCRAPE_JOB_KIND,
        scrape_job_params(
            library,
            version,
            start_url,
            max_pages=max_pages,
            prefilter=prefilter,
            scope=scope,
            ecosystem=ecosystem,
            resolved_from=resolved_from,
            registry_confirmed=registry_confirmed,
        ),
    )


def start_refresh_job(
    mgr: JobManager,
    library: str,
    version: str,
    start_url: str,
    *,
    max_pages: int | None = None,
    prefilter: bool = False,
    scope: dict[str, Any] | None = None,
    ecosystem: str = "custom",
) -> int:
    """Submit a refresh job to the queue, reusing the scrape job kind for incremental semantics."""
    return mgr.submit(
        SCRAPE_JOB_KIND,
        scrape_job_params(
            library,
            version,
            start_url,
            max_pages=max_pages,
            prefilter=prefilter,
            scope=scope,
            ecosystem=ecosystem,
            resolved_from="refresh",
            registry_confirmed=True,
        ),
    )


def build_scrape_runner(settings, conn, *, vec_store=None, embed=None, mgr=None):
    """Build the job consumer function that runs a full-site scrape per job params."""
    from libref.services.docs.ingest import IngestService

    service = IngestService(settings, conn, vec_store=vec_store, embed=embed)

    async def runner(job: Job):
        if job.kind != SCRAPE_JOB_KIND:
            raise ValueError(f"unsupported job kind: {job.kind}")
        params = job.params
        heartbeat = (lambda: mgr.heartbeat(job.id)) if mgr is not None else None
        return await service.scrape_docs_async(
            params["library"],
            params["version"],
            params["start_url"],
            max_pages=params.get("max_pages"),
            prefilter=params.get("prefilter", False),
            scope=params.get("scope"),
            ecosystem=params.get("ecosystem", "custom"),
            resolved_from=params.get("resolved_from"),
            registry_confirmed=params.get("registry_confirmed", True),
            heartbeat=heartbeat,
        )

    return runner


def run_scrape_job_sync(
    mgr: JobManager,
    settings,
    conn,
    job_id: int,
    *,
    timeout: float = 300.0,
    vec_store=None,
    embed: EmbedFn | None = None,
) -> Job:
    """Drive a single job synchronously: start worker, wait for settle, stop worker; for the CLI's async toggle."""

    async def _drive() -> Job:
        mgr.start_worker(
            build_scrape_runner(
                settings, conn, vec_store=vec_store, embed=embed, mgr=mgr
            )
        )
        try:
            return await mgr.wait_for(job_id, timeout=timeout)
        finally:
            mgr.stop_worker()

    return asyncio.run(_drive())
