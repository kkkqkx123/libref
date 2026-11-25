"""Web routes for feedback."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request

from libref.services.infra.auth import (
    require_any_scope,
)
from libref.web.api import (
    FeedbackCreate,
    FeedbackResponse,
    FeedbackStatus,
    OkIdResponse,
    OkResponse,
    envelope,
)
from libref.web.errors import ApiRejection
from libref.web.request_context import (
    _guard,
    _principal_id,
    _tenant_of,
    _write_tenant_of,
    close_rc,
)


def register(app, ctx) -> None:
    conn = ctx.conn
    _need = ctx.need
    _req_bundle = ctx.req_bundle

    @app.post("/feedback", response_model=OkIdResponse)
    def feedback_create(
        body: FeedbackCreate,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "feedback:write")
        from libref.services.ops.audit import write_audit
        from libref.services.ops.feedback import FeedbackService

        rc, should_close, est, bundle = _req_bundle(request, principal)
        svc = FeedbackService(rc)
        try:
            fid = svc.submit(
                tenant_id=_write_tenant_of(principal),
                principal_id=_principal_id(principal),
                entity_type=body.entity_type,
                entity_id=body.entity_id,
                kind=body.kind,
                body=body.body,
            )
        except ValueError as exc:
            raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        write_audit(
            rc,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="feedback.create",
            target=str(fid),
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        close_rc(rc, should_close)
        return {"id": fid, "ok": True}

    @app.get("/feedback", response_model=FeedbackResponse)
    def feedback_list(
        request: Request,
        limit: int = 50,
        status: FeedbackStatus | None = None,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        scope = require_any_scope(principal, "feedback:read", "feedback:read:own")
        own_only = scope == "feedback:read:own"
        author_id = _principal_id(principal)
        if own_only and author_id is None:
            return envelope([])
        from libref.services.ops.feedback import FeedbackService

        rc, should_close, est, bundle = _req_bundle(request, principal)
        svc = FeedbackService(rc)
        items = svc.list_items(
            tenant_id=_tenant_of(principal),
            limit=max(1, min(limit, 200)),
            status=status,
            principal_id=author_id if own_only else None,
        )
        return envelope(items)

    @app.post("/feedback/{feedback_id}/resolve", response_model=OkResponse)
    def feedback_resolve(
        feedback_id: int,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "feedback:resolve")
        from libref.services.ops.audit import write_audit
        from libref.services.ops.feedback import FeedbackService

        rc, should_close, est, bundle = _req_bundle(request, principal)
        svc = FeedbackService(rc)
        try:
            svc.resolve(
                feedback_id,
                resolved_by=_principal_id(principal),
                tenant_id=_tenant_of(principal),
            )
        except ValueError as exc:
            raise ApiRejection(404, "not_found", str(exc)) from exc
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="feedback.resolve",
            target=str(feedback_id),
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        return {"ok": True}
