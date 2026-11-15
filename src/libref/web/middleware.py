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
from libref.services.infra.auth import current_principal


def _public_path(path: str) -> bool:
    return path in ("/health", "/readyz") or path.startswith("/health")


class AuthMiddleware(BaseHTTPMiddleware):
    """Resolve bearer keys to principals and expose request context."""

    def __init__(self, app: Any, context) -> None:
        super().__init__(app)
        self._context = context

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
            response.headers["x-request-id"] = request_id
            return response
        if auth_mode == "dev":
            from libref.services.infra.auth import dev_principal

            principal = dev_principal(settings)
            request.state.principal = principal
            observe.principal_var.set(str(principal.id or principal.name))
            observe.tenant_var.set(principal.tenant_id)
            current_principal.set(principal)
            response = await call_next(request)
            observe.status_var.set(int(getattr(response, "status_code", 0) or 0))
            observe.duration_var.set(int((time.monotonic() - started) * 1000))
            response.headers["x-request-id"] = request_id
            return response
        origin = request.headers.get("origin")
        host = str(getattr(settings, "web_host", "127.0.0.1") or "127.0.0.1")
        if not is_allowed_origin(origin, host=host):
            return JSONResponse(
                {"error": "forbidden", "request_id": request_id},
                status_code=403,
                headers={"x-request-id": request_id},
            )
        raw = parse_bearer(request.headers.get("authorization"))
        if not raw:
            return JSONResponse(
                {"error": "unauthorized", "request_id": request_id},
                status_code=401,
                headers={"x-request-id": request_id},
            )
        try:
            import anyio as _anyio

            def _lookup() -> Any:
                try:
                    conn = self._context.connect(check_same_thread=False)
                except Exception:
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
        except Exception:
            return JSONResponse(
                {"error": "unavailable", "request_id": request_id},
                status_code=503,
                headers={"x-request-id": request_id},
            )
        if principal is None:
            return JSONResponse(
                {"error": "unauthorized", "request_id": request_id},
                status_code=401,
                headers={"x-request-id": request_id},
            )
        request.state.principal = principal
        observe.principal_var.set(str(principal.id or principal.name))
        observe.tenant_var.set(principal.tenant_id)
        current_principal.set(principal)
        response = await call_next(request)
        observe.status_var.set(int(getattr(response, "status_code", 0) or 0))
        observe.duration_var.set(int((time.monotonic() - started) * 1000))
        response.headers["x-request-id"] = request_id
        elapsed_ms = int((time.monotonic() - started) * 1000)
        response.headers["x-elapsed-ms"] = str(elapsed_ms)
        return response
