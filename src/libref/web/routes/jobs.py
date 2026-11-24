"""Web routes for jobs."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request

from libref.web.api import (
    JobCreate,
    OkIdResponse,
    OkResponse,
)
from libref.web.errors import ApiRejection
from libref.web.request_context import (
    _guard,
    _principal_id,
    _tenant_of,
    _write_tenant_of,
)


def register(app, ctx) -> None:
    _need = ctx.need
    _req_bundle = ctx.req_bundle
    _quota_gate = ctx.quota_gate

    @app.post("/jobs", response_model=OkIdResponse)
    def jobs_create(
        body: JobCreate,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "jobs:write")
        _quota_gate(request, principal, "job_submissions")
        from libref.services.ops.audit import write_audit

        rc, should_close, est, bundle = _req_bundle(request, principal)
        mgr = bundle["jobs"]
        job_id = mgr.submit(
            body.kind,
            body.params.model_dump(exclude_none=True),
            tenant_id=_write_tenant_of(principal),
            principal_id=_principal_id(principal),
        )
        write_audit(
            rc,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="jobs.create",
            target=f"{body.kind}:{job_id}",
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        if should_close:
            try:
                rc.close()
            except Exception:  # noqa: BLE001, S110
                pass
        return {"id": job_id, "ok": True}

    @app.post("/jobs/{job_id}/cancel", response_model=OkResponse)
    def jobs_cancel(
        job_id: int,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "jobs:write")
        from libref.services.ops.audit import write_audit
        from libref.services.ops.index_requests import INDEX_JOB_KINDS

        rc, should_close, est, bundle = _req_bundle(request, principal)
        mgr = bundle["jobs"]
        try:
            job = mgr.get(job_id)
            if job is not None and job.kind in INDEX_JOB_KINDS:
                raise ApiRejection(
                    400,
                    "invalid_argument",
                    "indexing jobs must be cancelled through index requests",
                )
            mgr.cancel(job_id, tenant_id=_tenant_of(principal))
        except ValueError as exc:
            raise ApiRejection(404, "not_found", str(exc)) from exc
        write_audit(
            rc,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="jobs.cancel",
            target=str(job_id),
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        if should_close:
            try:
                rc.close()
            except Exception:  # noqa: BLE001, S110
                pass
        return {"ok": True}
