"""Web routes for index requests."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request

from libref.web.api import (
    IndexRequestCreate,
    IndexRequestItem,
    IndexRequestReject,
    IndexRequestResponse,
    IndexRequestStatus,
    envelope,
)
from libref.web.request_context import (
    _guard,
    _index_request_error,
    _tenant_of,
)


def register(app, ctx) -> None:
    _need = ctx.need
    _req_bundle = ctx.req_bundle

    @app.post("/index-requests", response_model=IndexRequestItem)
    async def index_request_create(
        body: IndexRequestCreate,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "index:request")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return await bundle["index_requests"].submit(
                    body.model_dump(exclude_none=True),
                    principal,
                    tenant_id=_tenant_of(principal),
                    request_id=str(getattr(request.state, "request_id", "")),
                )
            except Exception as exc:
                from libref.services.ops.index_requests import IndexRequestError

                if isinstance(exc, IndexRequestError):
                    raise _index_request_error(exc) from exc
                raise
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/index-requests", response_model=IndexRequestResponse)
    def index_request_list(
        request: Request,
        status: IndexRequestStatus | None = None,
        limit: int = 50,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "index:request")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                items = bundle["index_requests"].list(
                    principal,
                    tenant_id=_tenant_of(principal),
                    status=status,
                    limit=limit,
                )
            except Exception as exc:
                from libref.services.ops.index_requests import IndexRequestError

                if isinstance(exc, IndexRequestError):
                    raise _index_request_error(exc) from exc
                raise
            return envelope(items)
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/index-requests/pending", response_model=IndexRequestResponse)
    def index_request_pending(
        request: Request,
        limit: int = 50,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "index:approve")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            items = bundle["index_requests"].list(
                principal,
                tenant_id=_tenant_of(principal),
                pending=True,
                limit=limit,
            )
            return envelope(items)
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/index-requests/{request_id}", response_model=IndexRequestItem)
    def index_request_show(
        request_id: int,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return bundle["index_requests"].get(
                    request_id, principal, tenant_id=_tenant_of(principal)
                )
            except Exception as exc:
                from libref.services.ops.index_requests import IndexRequestError

                if isinstance(exc, IndexRequestError):
                    raise _index_request_error(exc) from exc
                raise
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.post("/index-requests/{request_id}/cancel", response_model=IndexRequestItem)
    def index_request_cancel(
        request_id: int,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "index:request")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return bundle["index_requests"].cancel(
                    request_id,
                    principal,
                    tenant_id=_tenant_of(principal),
                    request_id_header=str(getattr(request.state, "request_id", "")),
                )
            except Exception as exc:
                from libref.services.ops.index_requests import IndexRequestError

                if isinstance(exc, IndexRequestError):
                    raise _index_request_error(exc) from exc
                raise
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.post("/index-requests/{request_id}/approve", response_model=IndexRequestItem)
    def index_request_approve(
        request_id: int,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "index:approve")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return bundle["index_requests"].approve(
                    request_id,
                    principal,
                    tenant_id=_tenant_of(principal),
                    request_id_header=str(getattr(request.state, "request_id", "")),
                )
            except Exception as exc:
                from libref.services.ops.index_requests import IndexRequestError

                if isinstance(exc, IndexRequestError):
                    raise _index_request_error(exc) from exc
                raise
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.post("/index-requests/{request_id}/reject", response_model=IndexRequestItem)
    def index_request_reject(
        request_id: int,
        body: IndexRequestReject,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "index:approve")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return bundle["index_requests"].reject(
                    request_id,
                    body.reason,
                    principal,
                    tenant_id=_tenant_of(principal),
                    request_id_header=str(getattr(request.state, "request_id", "")),
                )
            except Exception as exc:
                from libref.services.ops.index_requests import IndexRequestError

                if isinstance(exc, IndexRequestError):
                    raise _index_request_error(exc) from exc
                raise
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass
