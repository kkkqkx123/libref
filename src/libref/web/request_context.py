"""Shared request helpers and service bundle assembly for Web routes."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from libref.services.infra.auth import (
    PLATFORM_SCOPE,
    require_scopes,
    write_tenant,
)
from libref.services.infra.jobs import JobManager
from libref.services.infra.tenant import GLOBAL_TENANT
from libref.web.errors import ApiRejection, known_error_code


def _guard(request: Request):
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise ApiRejection(401, "unauthorized", "unauthorized")
    return principal


def _need(principal, *scopes: str) -> None:
    require_scopes(principal, *scopes)


def _principal_id(principal) -> int | None:
    pid = getattr(principal, "id", None)
    return int(pid) if isinstance(pid, int) else None


def _tenant_of(principal) -> str:
    scopes = set(getattr(principal, "scopes", set()) or ())
    if PLATFORM_SCOPE in scopes:
        return GLOBAL_TENANT
    return str(getattr(principal, "tenant_id", "default") or "default")


def _write_tenant_of(principal) -> str:
    return write_tenant(principal)


def _index_request_error(exc: Exception) -> HTTPException:
    """Translate service failures while preserving the stable error code."""
    from libref.services.ops.index_requests import IndexRequestError, QuotaDenied

    assert isinstance(exc, IndexRequestError)
    if isinstance(exc, QuotaDenied):
        detail = {
            "error": "quota exceeded",
            "detail": exc.message,
            "resource": exc.resource,
            "reset_at": exc.reset_at,
        }
        return ApiRejection(exc.status, known_error_code(exc.code), detail)
    return ApiRejection(exc.status, known_error_code(exc.code), exc.message)


def _read_guard(principal=Depends(_guard)):
    _need(principal, "read")


def close_rc(rc, should_close: bool) -> None:
    """Close a request-scoped connection opened by ``req_bundle`` when owned."""
    if should_close:
        try:
            rc.close()
        except Exception:  # noqa: BLE001, S110
            pass


class WebRequestContext:
    """Builder-owned dependencies shared by every route module."""

    def __init__(
        self,
        conn,
        settings,
        context,
        svc,
        discovery,
        pages,
        catalog,
        mining,
        review,
        rerank,
        chat,
        vec_store,
        embed,
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.context = context
        self.svc = svc
        self.discovery = discovery
        self.pages = pages
        self.catalog = catalog
        self.mining = mining
        self.review = review
        self.rerank = rerank
        self.chat = chat
        self.vec_store = vec_store
        self.embed = embed

    def need(self, principal, *scopes: str) -> None:
        require_scopes(principal, *scopes)

    def eff_settings(self, request=None):
        settings = self.settings
        try:
            st = getattr(
                getattr(getattr(request, "app", None), "state", None), "settings", None
            )
            if st is not None:
                return st
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            ctx = getattr(
                getattr(getattr(request, "app", None), "state", None), "context", None
            )
            if ctx is not None and getattr(ctx, "settings", None) is not None:
                return ctx.settings
        except Exception:  # noqa: BLE001, S110
            pass
        return settings

    def eff_context(self, request=None):
        context = self.context
        try:
            ctx = getattr(
                getattr(getattr(request, "app", None), "state", None), "context", None
            )
            if ctx is not None:
                return ctx
        except Exception:  # noqa: BLE001, S110
            pass
        return context

    def req_bundle(self, request, principal):
        conn = self.conn
        svc = self.svc
        pages = self.pages
        catalog = self.catalog
        mining = self.mining
        review = self.review
        rerank = self.rerank
        chat = self.chat
        vec_store = self.vec_store
        embed = self.embed
        _eff_settings = self.eff_settings
        _eff_context = self.eff_context
        est = _eff_settings(request)
        ectx = _eff_context(request)
        if ectx is not None:
            rc = ectx.connect(check_same_thread=False)
            bundle = ectx.services_for(rc)
            # Override settings inside bundle with latest effective settings
            return rc, True, est, bundle
        # Fallback single-connection path for tests without context
        from libref.services.docs.doc_insight import DocInsightService
        from libref.services.ops.index_requests import IndexRequestService

        jobs = JobManager(conn)
        fb = {
            "search": svc,
            "pages": pages,
            "catalog": catalog,
            "mining": mining,
            "doc_insight": DocInsightService(conn, est),
            "review": review,
            "index_requests": IndexRequestService(conn, est, jobs=jobs),
            "jobs": jobs,
            "rerank": rerank,
            "chat": chat,
            "gateway": None,
            "vec_store": vec_store,
            "embed": embed,
            "settings": est,
        }
        return conn, False, est, fb

    def quota_gate(
        self, request: Request, principal, resource: str, cost: int = 1
    ) -> None:
        _eff_settings = self.eff_settings
        _req_bundle = self.req_bundle
        """Reject the submission with 429 when the subject is over quota."""
        est = _eff_settings(request)
        if not bool(getattr(est, "quota_enabled", False)):
            return
        from libref.services.infra import quota as _quota

        rc, should_close, _, _bundle = _req_bundle(request, principal)
        try:
            decision = _quota.enforce(
                rc,
                tenant_id=_write_tenant_of(principal),
                principal_id=_principal_id(principal),
                resource=resource,
                cost=cost,
                settings=est,
                request_id=str(getattr(request.state, "request_id", "")),
            )
            if decision is not None:
                raise ApiRejection(
                    429,
                    "quota_exceeded",
                    {
                        "error": "quota exceeded",
                        "resource": resource,
                        "reset_at": decision.reset_at,
                        "request_id": str(getattr(request.state, "request_id", "")),
                    },
                )
        finally:
            close_rc(rc, should_close)
