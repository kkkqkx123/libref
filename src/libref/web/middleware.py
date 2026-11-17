"""Unified authentication for combined serving; one chain for both exits."""

from __future__ import annotations

import time
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from libref import observe
from libref.mcp_server.auth import is_allowed_origin, parse_bearer
from libref.services.infra import metrics as request_metrics
from libref.services.infra.auth import current_principal
from libref.web.errors import error_body


def _public_path(path: str) -> bool:
    return path in ("/health", "/readyz", "/") or path.startswith(
        ("/health", "/assets/")
    )


def _reject(status: int, detail: Any, code: str, request_id: str) -> JSONResponse:
    """Single rejection shape: same builder as route-level ``HTTPException``.

    The middleware knows its causes outright, so the code is passed in instead of
    being re-derived from the detail text.
    """
    return JSONResponse(
        error_body(status, detail, request_id, code=code),
        status_code=status,
        headers={"x-request-id": request_id},
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """Resolve bearer keys to principals and expose request context."""

    def __init__(self, app: Any, context) -> None:
        super().__init__(app)
        self._context = context

    def _quota_gate(self, request: Request, principal) -> JSONResponse | None:
        """Count the request and enforce http_requests quota; None means allowed."""
        settings = self._context.settings
        if not bool(getattr(settings, "quota_enabled", False)):
            return None
        from libref.services.infra import quota
        from libref.services.ops.audit import write_audit

        tenant_id = str(principal.tenant_id or "default")
        try:
            conn = self._context.connect(check_same_thread=False)
        except Exception:  # noqa: BLE001
            return None  # fail open: bookkeeping never becomes an outage
        try:
            decision = quota.check(
                conn,
                tenant_id=tenant_id,
                principal_id=principal.id,
                resource="http_requests",
                settings=settings,
            )
            if not decision.allowed:
                request_metrics.incr("quota_denied_total{resource=http_requests}")
                quota.record_event(
                    conn,
                    tenant_id=tenant_id,
                    principal_id=principal.id,
                    resource="http_requests",
                    result="denied",
                    used=decision.used,
                    limit_value=decision.limit_value,
                    request_id=str(getattr(request.state, "request_id", "")),
                )
                write_audit(
                    conn,
                    tenant_id=tenant_id,
                    principal_id=principal.id,
                    action="quota",
                    target="http_requests",
                    result="denied",
                    request_id=str(getattr(request.state, "request_id", "")),
                )
                rid = str(getattr(request.state, "request_id", ""))
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
            quota.add_usage(
                conn,
                tenant_id=tenant_id,
                principal_id=principal.id,
                resource="http_requests",
            )
            return None
        except Exception:  # noqa: BLE001 — quota bookkeeping must not block serving
            return None
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001, S110 — best effort close
                pass

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
        settings = self._context.settings
        auth_mode = str(getattr(settings, "auth_mode", "dev") or "dev")
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
        if auth_mode == "dev":
            from libref.services.infra.auth import dev_principal

            principal = dev_principal(settings)
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
            response = await call_next(request)
            observe.status_var.set(int(getattr(response, "status_code", 0) or 0))
            observe.duration_var.set(int((time.monotonic() - started) * 1000))
            self._record_metrics(
                request.url.path,
                int(getattr(response, "status_code", 0) or 0),
                int((time.monotonic() - started) * 1000),
            )
            response.headers["x-request-id"] = request_id
            return response
        origin = request.headers.get("origin")
        host = str(getattr(settings, "web_host", "127.0.0.1") or "127.0.0.1")
        if not is_allowed_origin(origin, host=host):
            return _reject(403, "origin not allowed", "origin_forbidden", request_id)
        raw = parse_bearer(request.headers.get("authorization"))
        if not raw:
            return _reject(401, "unauthorized", "unauthorized", request_id)
        try:
            import anyio as _anyio

            def _lookup() -> Any:
                try:
                    conn = self._context.connect(check_same_thread=False)
                except Exception:  # noqa: BLE001
                    return None
                try:
                    from libref.services.infra.auth import lookup_principal

                    return lookup_principal(conn, raw)
                finally:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001, S110 — best effort close
                        pass

            principal = await _anyio.to_thread.run_sync(_lookup)
        except Exception:  # noqa: BLE001
            return _reject(503, "auth backend unavailable", "unavailable", request_id)
        if principal is None:
            return _reject(401, "unauthorized", "unauthorized", request_id)
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
        response = await call_next(request)
        observe.status_var.set(int(getattr(response, "status_code", 0) or 0))
        observe.duration_var.set(int((time.monotonic() - started) * 1000))
        self._record_metrics(
            request.url.path,
            int(getattr(response, "status_code", 0) or 0),
            int((time.monotonic() - started) * 1000),
        )
        response.headers["x-request-id"] = request_id
        elapsed_ms = int((time.monotonic() - started) * 1000)
        response.headers["x-elapsed-ms"] = str(elapsed_ms)
        return response
