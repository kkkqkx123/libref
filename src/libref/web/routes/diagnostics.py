"""Web routes for diagnostics."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request

from libref.config import settings_fingerprint
from libref.web.api import (
    HealthResponse,
    MetricsResponse,
    PrincipalResponse,
    ReadyResponse,
)
from libref.web.request_context import (
    _guard,
    _principal_id,
    _tenant_of,
)


def register(app, ctx) -> None:
    conn = ctx.conn
    _need = ctx.need
    _eff_settings = ctx.eff_settings
    _eff_context = ctx.eff_context

    @app.get("/health", response_model=HealthResponse)
    def health(request: Request) -> dict[str, Any]:
        return {"ok": True, "fingerprint": settings_fingerprint(_eff_settings(request))}

    @app.get("/me", response_model=PrincipalResponse)
    def me(principal=Depends(_guard)) -> dict[str, Any]:
        return {
            "id": _principal_id(principal),
            "name": str(getattr(principal, "name", "")),
            "kind": str(getattr(principal, "kind", "")),
            "tenant_id": _tenant_of(principal),
            "roles": list(getattr(principal, "roles", []) or []),
            "scopes": sorted(set(getattr(principal, "scopes", set()) or set())),
        }

    @app.get("/metrics", response_model=MetricsResponse)
    def metrics_route(principal=Depends(_guard)) -> dict[str, Any]:
        """Read-only process counters: requests, quota denials, token usage, latency."""
        _need(principal, "read")
        from libref.services.infra.metrics import snapshot as _snapshot

        snapshot = _snapshot()
        counters = {
            key: int(value)
            for key, value in snapshot.items()
            if key not in ("uptime_seconds", "duration_ms_p50", "duration_ms_p95")
        }
        return {
            "uptime_seconds": int(snapshot["uptime_seconds"]),
            "duration_ms_p50": snapshot.get("duration_ms_p50"),
            "duration_ms_p95": snapshot.get("duration_ms_p95"),
            "counters": counters,
        }

    @app.get("/readyz", response_model=ReadyResponse)
    def readyz(request: Request) -> dict[str, Any]:
        est = _eff_settings(request)
        ectx = _eff_context(request)
        checks: dict[str, Any] = {}
        _rc = None
        try:
            if ectx is not None:
                _rc = ectx.connect(check_same_thread=False)
                _rc.execute("SELECT 1").fetchone()
            else:
                conn.execute("SELECT 1").fetchone()
            checks["sqlite"] = "ok"
        except Exception as exc:  # noqa: BLE001 — readiness reports degraded
            checks["sqlite"] = f"unavailable: {exc}"
        finally:
            if _rc is not None:
                try:
                    _rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass
        try:
            if ectx is not None:
                vec = ectx.vec_store
                emb = ectx.embed
                gw = ectx.gateway
            else:
                from libref.services.infra.vectors import build_vector as _build_vector

                vec, emb, gw = _build_vector(est)
            checks["vector"] = "disabled" if vec is None else "ok"
            checks["embed"] = "disabled" if emb is None else "ok"
            chat_fn = gw.chat_fn() if gw is not None else None
            checks["chat"] = "disabled" if chat_fn is None else "ok"
        except Exception as exc:  # noqa: BLE001 — vector optional
            checks["vector"] = f"unavailable: {exc}"
        degraded = any(v != "ok" and v != "disabled" for v in checks.values())
        out: dict[str, Any] = {
            "ok": not degraded,
            "checks": checks,
            "fingerprint": settings_fingerprint(est),
            "generation": getattr(ectx, "generation", 0) if ectx is not None else 0,
        }
        if degraded:
            out["degraded"] = True
        return out
