"""MCP tools for ops."""

from __future__ import annotations

from typing import Any, Literal

from libref.errors import LibrefError
from libref.limits import (
    INSIGHT_ROWS,
    LISTING_ROWS,
    require_limit,
)
from libref.mcp_server.context import (
    _as_error,
    _invalid,
    _items,
    _not_found,
)
from libref.mcp_server.contract import ToolFailure


def register(mcp, ctx, wrap) -> dict[str, object]:
    conn = ctx.conn
    _principal = ctx.principal
    _require = ctx.require
    _tenant = ctx.tenant
    _make_bundle = ctx.make_bundle
    _close = ctx.close

    def review(
        action: Literal["list", "approve", "conflict", "reopen"],
        entity_id: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Review workflow: list pending items or transition one item."""
        principal = _principal()
        need = "review:read" if action == "list" else "review:write"
        _require(principal, need)
        require_limit(limit, low=1, high=INSIGHT_ROWS)
        if action != "list" and entity_id is None:
            raise _invalid(f"{action} requires entity_id")
        _rcr, _bundler, _scr = _make_bundle()
        try:
            try:
                result = _bundler["review"].apply(
                    action,
                    entity_id,
                    limit,
                    tenant_id=_tenant(principal),
                    principal_id=getattr(principal, "id", None),
                )
                if action != "list":
                    from libref.services.ops.audit import write_audit

                    write_audit(
                        _rcr,
                        tenant_id=_tenant(principal),
                        principal_id=getattr(principal, "id", None),
                        action=f"review.{action}",
                        target=str(entity_id or ""),
                        request_id="",
                    )
            except ValueError as exc:
                text = str(exc)
                if "cannot transition" in text or "cannot reopen" in text:
                    raise ToolFailure("review_conflict", text, field="action") from exc
                raise _invalid(text) from exc
            except Exception as exc:
                raise _as_error(exc) from exc
        finally:
            _close(_rcr, _scr)
        if isinstance(result, list):
            return _items(result)
        return result

    def job_show(job_id: int | None = None, limit: int = 20) -> dict[str, Any]:
        """Show job results, listing recent jobs when no id is given."""
        principal = _principal()
        require_limit(limit, low=1, high=LISTING_ROWS)
        _rcj, _bundlej, _scj = _make_bundle()
        try:
            try:
                return _bundlej.get(
                    "jobs",
                    __import__(
                        "libref.services.infra.jobs", fromlist=["JobManager"]
                    ).JobManager(_rcj),
                ).show(job_id, limit, tenant_id=_tenant(principal))
            finally:
                _close(_rcj, _scj)
        except LibrefError as exc:
            if exc.code == "not_found":
                return _not_found(exc.message)
            raise
        except Exception as exc:
            raise _as_error(exc) from exc

    def submit_feedback(
        body: str,
        entity_type: str | None = None,
        entity_id: int | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """Submit developer feedback without touching review state."""
        principal = _principal()
        _require(principal, "feedback:write")
        if not (body or "").strip():
            raise _invalid("body is empty", field="body")
        try:
            from libref.services.ops.audit import write_audit
            from libref.services.ops.feedback import FeedbackService

            svc = FeedbackService(conn)
            fid = svc.submit(
                tenant_id=_tenant(principal),
                principal_id=getattr(principal, "id", None),
                entity_type=entity_type or "call_instance",
                entity_id=int(entity_id or 0),
                kind=kind or "general",
                body=body,
            )
            write_audit(
                conn,
                tenant_id=_tenant(principal),
                principal_id=getattr(principal, "id", None),
                action="feedback.create",
                target=str(fid),
                request_id="",
            )
        except Exception as exc:
            raise _as_error(exc) from exc
        return {"id": fid, "ok": True}

    return {
        "review": review,
        "job_show": job_show,
        "submit_feedback": submit_feedback,
    }
