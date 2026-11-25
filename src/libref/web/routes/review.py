"""Web routes for review."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import Depends, Request

from libref.web.api import (
    OkResponse,
    ReviewAction,
    ReviewQuery,
    ReviewResponse,
    envelope,
)
from libref.web.errors import ApiRejection
from libref.web.request_context import (
    _guard,
    _principal_id,
    _tenant_of,
    close_rc,
)


def register(app, ctx) -> None:
    _need = ctx.need
    _req_bundle = ctx.req_bundle

    @app.get("/review", response_model=ReviewResponse)
    def review_list(
        request: Request,
        action: Literal["list"] = "list",
        limit: int = 50,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "review:read")
        params = ReviewQuery(action=action, limit=limit)
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                items = bundle["review"].apply(
                    params.action,
                    None,
                    params.limit,
                    tenant_id=_tenant_of(principal),
                )
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
            return envelope(items)
        finally:
            close_rc(rc, should_close)

    @app.post("/review", response_model=OkResponse)
    def review_apply(
        body: ReviewAction, request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "review:write")
        from libref.services.ops.audit import write_audit

        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                bundle["review"].apply(
                    body.action,
                    body.entity_id,
                    body.limit,
                    tenant_id=_tenant_of(principal),
                    principal_id=_principal_id(principal),
                )
            except ValueError as exc:
                text = str(exc)
                if "cannot transition" in text or "cannot reopen" in text:
                    raise ApiRejection(409, "review_conflict", text) from exc
                raise ApiRejection(400, "invalid_argument", text) from exc
            write_audit(
                rc,
                tenant_id=_tenant_of(principal),
                principal_id=_principal_id(principal),
                action=f"review.{body.action}",
                target=str(body.entity_id or ""),
                request_id=str(
                    getattr(getattr(request, "state", None), "request_id", "")
                ),
            )
            return {"ok": True}
        finally:
            close_rc(rc, should_close)
