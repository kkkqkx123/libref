"""Read-only request listing, detail and next-step discovery."""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Any

from libref.services.infra.auth import PLATFORM_SCOPE, Principal
from libref.services.ops.index_requests.models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    IndexRequestError,
    _actor_id,
    _actor_name,
    _has,
)


class Queries:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    def list(
        self,
        principal: Principal | None,
        *,
        tenant_id: str | None = None,
        status: str | None = None,
        pending: bool = False,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        """List the caller's requests, or the pending queue for approvers."""
        if limit <= 0:
            raise IndexRequestError("limit must be positive")
        if pending and not _has(principal, "index:approve"):
            raise IndexRequestError(
                "missing scope: index:approve", code="forbidden", status=403
            )
        if status is not None and status not in ACTIVE_STATUSES | TERMINAL_STATUSES:
            raise IndexRequestError(f"unsupported status: {status}")
        clauses: list[str] = []
        values: list[Any] = []
        visible_tenant = (
            tenant_id
            if tenant_id is not None
            else getattr(principal, "tenant_id", "default")
        )
        if visible_tenant != "*":
            clauses.append("tenant_id=?")
            values.append(visible_tenant)
        if pending:
            clauses.append("status='pending'")
        elif status is not None:
            clauses.append("status=?")
            values.append(status)
        platform = _has(principal, PLATFORM_SCOPE)
        approver = _has(principal, "index:approve")
        if not pending and not platform and not approver:
            pid = _actor_id(principal)
            name = _actor_name(principal)
            if pid is not None:
                clauses.append("principal_id=?")
                values.append(pid)
            else:
                clauses.append("requester=?")
                values.append(name)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._conn.execute(
            "SELECT id, tenant_id, principal_id, requester, kind, target, source,"
            " params, status, dedupe_key, approved_by, reject_reason, job_id, error,"
            " created_at, updated_at FROM index_requests"
            + where
            + " ORDER BY id DESC LIMIT ?",
            (*values, min(int(limit), 200)),
        ).fetchall()
        return [self._item(row) for row in rows]

    def get(
        self,
        request_id: int,
        principal: Principal | None,
        *,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Read one request visible to its owner or an approver."""
        item = self._row(request_id)
        if item is None:
            raise IndexRequestError(
                f"unknown index request: {request_id}", status=404, code="not_found"
            )
        visible_tenant = (
            tenant_id
            if tenant_id is not None
            else getattr(principal, "tenant_id", "default")
        )
        if not self._visible(item, principal, visible_tenant):
            raise IndexRequestError(
                f"unknown index request: {request_id}", status=404, code="not_found"
            )
        return item

    @staticmethod
    def next_tools(item: dict[str, Any]) -> builtins.list[str]:
        """Suggested follow-up tools so clients need not guess the next step."""
        if item["status"] == "done":
            return (
                ["catalog", "search", "api_insight"]
                if item["kind"] == "repo"
                else [
                    "catalog",
                    "read_doc",
                    "search",
                ]
            )
        if item["status"] in ACTIVE_STATUSES:
            return ["wait_index_request", "index_request_status"]
        if item["status"] == "failed":
            return [
                "index_request_status",
                "request_repo_index"
                if item["kind"] == "repo"
                else "request_docs_index",
            ]
        return ["index_request_status"]
