"""Unified authentication for combined serving; one chain for both exits."""

from __future__ import annotations

import time
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from libref import observe
from libref.services.infra import metrics as request_metrics
from libref.services.infra.auth import (
    AuthDenied,
    authenticate_request,
    current_principal,
    write_tenant,
)
from libref.web.errors import KnownErrorCode, error_body, known_error_code


def _public_path(path: str) -> bool:
    return path in ("/health", "/readyz", "/") or path.startswith("/assets/")


def _reject(
    status: int, detail: Any, code: KnownErrorCode, request_id: str
) -> JSONResponse:
    """Single rejection shape: same builder as route-level ``HTTPException``."""
    return JSONResponse(
        error_body(status, detail, request_id, code=code),
        status_code=status,
        headers={"x-request-id": request_id},
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """Resolve requests to principals and expose request context."""

    def __init__(
        self,
        app: Any,
        context=None,
        *,
        conn=None,
        settings=None,
    ) -> None:
        super().__init__(app)
        self._context = context
        self._conn = conn
        self._settings = settings

    def _context_for(self, request: Request | None):
        if request is not None:
            current = getattr(getattr(request.app, "state", None), "context", None)
            if current is not None:
                return current
        return self._context

    def _settings_snapshot(self, request: Request | None = None):
        context = self._context_for(request)
        if context is not None:
            return context.settings
        if request is not None:
            current = getattr(getattr(request.app, "state", None), "settings", None)
            if current is not None:
                return current
        return self._settings

    def _connect(self, request: Request | None = None):
        """Return a connection and whether this middleware owns its lifetime."""
        context = self._context_for(request)
        if context is not None:
            return context.connect(check_same_thread=False), True
        return self._conn, False

    @staticmethod
    def _close(conn, owned: bool) -> None:
        if not owned:
            return
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110 — best effort close
            pass

    def _audit_denial(
        self,
        request: Request,
        code: str,
        *,
        action: str = "auth.denied",
        principal=None,
        target: str | None = None,
    ) -> None:
        """Record authorization failures without turning them into outages."""
        from libref.services.ops.audit import write_audit

        try:
            conn, owned = self._connect(request)
        except Exception:  # noqa: BLE001 — audit must not block rejections
            return
        try:
            write_audit(
                conn,
                tenant_id=write_tenant(principal),
                principal_id=getattr(principal, "id", None),
                action=action,
                target=target or request.url.path,
                result="denied" if action == "auth.forbidden" else code,
                request_id=str(getattr(request.state, "request_id", "")),
            )
        finally:
            self._close(conn, owned)

    def _quota_gate(self, request: Request, principal) -> JSONResponse | None:
        """Count the request and enforce http_requests quota; None means allowed."""
        settings = self._settings_snapshot(request)
        if not bool(getattr(settings, "quota_enabled", False)):
            return None
        from libref.services.infra import quota

        rid = str(getattr(request.state, "request_id", ""))
        try:
            conn, owned = self._connect(request)
        except Exception:  # noqa: BLE001
            return None  # fail open: bookkeeping never becomes an outage
        try:
            decision = quota.enforce(
                conn,
                tenant_id=write_tenant(principal),
                principal_id=principal.id,
                resource="http_requests",
                settings=settings,
                request_id=rid,
            )
            if decision is None:
                return None
            request_metrics.incr("quota_denied_total{resource=http_requests}")
            return _reject(
                429,
                {
                    "error": "quota exceeded",
                    "resource": decision.resource,
                    "reset_at": decision.reset_at,
                    "request_id": rid,
                },
                "quota_exceeded",
                rid,
            )
        except Exception:  # noqa: BLE001 — quota bookkeeping must not block serving
            return None
        finally:
            self._close(conn, owned)

    def _record_metrics(self, route: str, status: int, elapsed_ms: int) -> None:
        request_metrics.incr(f"http_requests_total{{route={route},status={status}}}")
        request_metrics.record_duration(elapsed_ms)

    async def dispatch(self, request: Request, call_next):
        started = time.monotonic()
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        observe.request_id_var.set(request_id)
        observe.route_var.set(request.url.path)
        observe.principal_var.set("")
        observe.tenant_var.set("")
        observe.status_var.set(0)
        observe.duration_var.set(0)
        current_principal.set(None)
        if request.method == "OPTIONS":
            response = JSONResponse({}, status_code=200)
            response.headers["x-request-id"] = request_id
            return response
        if _public_path(request.url.path):
            response = await call_next(request)
            observe.status_var.set(int(getattr(response, "status_code", 0) or 0))
            observe.duration_var.set(int((time.monotonic() - started) * 1000))
            self._record_metrics(
                observe.route_var.get(""),
                observe.status_var.get(0),
                observe.duration_var.get(0),
            )
            response.headers["x-request-id"] = request_id
            return response
        try:
            conn, owned = self._connect(request)
        except Exception:  # noqa: BLE001
            return _reject(503, "auth backend unavailable", "unavailable", request_id)
        try:
            principal = authenticate_request(
                conn,
                self._settings_snapshot(request),
                authorization=request.headers.get("authorization"),
                origin=request.headers.get("origin"),
            )
        except AuthDenied as exc:
            self._audit_denial(request, exc.code)
            return _reject(
                exc.status, exc.detail, known_error_code(exc.code), request_id
            )
        finally:
            self._close(conn, owned)
        request.state.principal = principal
        observe.principal_var.set(str(principal.id or principal.name))
        observe.tenant_var.set(principal.tenant_id)
        current_principal.set(principal)
        gate = self._quota_gate(request, principal)
        if gate is not None:
            observe.status_var.set(429)
            self._record_metrics(
                request.url.path, 429, int((time.monotonic() - started) * 1000)
            )
            return gate
        try:
            response = await call_next(request)
        except AuthDenied as exc:
            self._audit_denial(
                request,
                exc.code,
                action="auth.forbidden",
                principal=principal,
                target=exc.detail,
            )
            response = _reject(
                exc.status, exc.detail, known_error_code(exc.code), request_id
            )
        status = int(getattr(response, "status_code", 0) or 0)
        observe.status_var.set(status)
        observe.duration_var.set(int((time.monotonic() - started) * 1000))
        self._record_metrics(request.url.path, status, observe.duration_var.get(0))
        response.headers["x-request-id"] = request_id
        response.headers["x-elapsed-ms"] = str(observe.duration_var.get(0))
        return response
