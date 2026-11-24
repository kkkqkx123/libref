"""Approval state transitions and indexing job reconciliation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from libref.services.infra.auth import Principal
from libref.services.infra.jobs import Job
from libref.services.ops.index_requests.models import (
    _TRANSITIONS,
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    IndexRequestError,
    _as_int,
    _has,
    _validate_docs_source,
    _validate_repo_source,
)


class Lifecycle:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    def _revalidate_target(self, item: dict[str, Any]) -> None:
        if item["kind"] == "repo":
            _validate_repo_source(item["source"], self._settings)
            return
        address = item["params"].get("start_url") or item["source"]
        _validate_docs_source(str(address), self._settings)

    def _require_actor(self, principal: Principal | None) -> None:
        if principal is None:
            raise IndexRequestError("unauthorized", code="unauthorized", status=401)

    def cancel(
        self,
        request_id: int,
        principal: Principal | None,
        *,
        tenant_id: str | None = None,
        request_id_header: str = "",
    ) -> dict[str, Any]:
        """Cancel a request before its job starts running."""
        self._require_actor(principal)
        item = self.get(request_id, principal, tenant_id=tenant_id)
        if not self._owned(item, principal):
            raise IndexRequestError(
                "only the requester may cancel a request", code="forbidden", status=403
            )
        if item["status"] not in ("pending", "approved", "queued"):
            raise IndexRequestError(
                f"cannot cancel request in {item['status']}",
                code="conflict",
                status=409,
            )
        if item["job_id"] is not None:
            job = self._jobs.get(int(item["job_id"]))
            if job is not None and job.status == "running":
                raise IndexRequestError(
                    "cannot cancel a running index job",
                    code="conflict",
                    status=409,
                )
            try:
                self._jobs.cancel(int(item["job_id"]), tenant_id=item["tenant_id"])
            except ValueError:
                pass
        self._transition(request_id, "cancelled", expected=item["status"])
        self._audit(
            principal,
            "index_request.cancel",
            str(request_id),
            tenant_id=item["tenant_id"],
            request_id=request_id_header,
        )
        return self._row(request_id) or item

    def approve(
        self,
        request_id: int,
        principal: Principal | None,
        *,
        tenant_id: str | None = None,
        request_id_header: str = "",
    ) -> dict[str, Any]:
        """Approve a pending request; approvers without ingest:write cannot self-approve."""
        self._require_actor(principal)
        if not _has(principal, "index:approve"):
            raise IndexRequestError(
                "missing scope: index:approve", code="forbidden", status=403
            )
        item = self.get(request_id, principal, tenant_id=tenant_id)
        if item["status"] != "pending":
            raise IndexRequestError(
                f"cannot approve request in {item['status']}",
                code="conflict",
                status=409,
            )
        if self._owned(item, principal) and not _has(principal, "ingest:write"):
            raise IndexRequestError(
                "cannot approve own request", code="forbidden", status=403
            )
        self._revalidate_target(item)
        result = self._queue(
            request_id,
            principal,
            tenant_id=item["tenant_id"],
            approved=True,
        )
        self._audit(
            principal,
            "index_request.approve",
            str(request_id),
            tenant_id=item["tenant_id"],
            result="queued" if result["job_id"] else "retry_pending",
            request_id=request_id_header,
        )
        return self.get(request_id, principal, tenant_id=tenant_id)

    def reject(
        self,
        request_id: int,
        reason: str,
        principal: Principal | None,
        *,
        tenant_id: str | None = None,
        request_id_header: str = "",
    ) -> dict[str, Any]:
        """Reject a pending request with a mandatory reason."""
        self._require_actor(principal)
        if not _has(principal, "index:approve"):
            raise IndexRequestError(
                "missing scope: index:approve", code="forbidden", status=403
            )
        if not reason.strip():
            raise IndexRequestError("reject reason is required")
        item = self.get(request_id, principal, tenant_id=tenant_id)
        if item["status"] != "pending":
            raise IndexRequestError(
                f"cannot reject request in {item['status']}",
                code="conflict",
                status=409,
            )
        if self._owned(item, principal) and not _has(principal, "ingest:write"):
            raise IndexRequestError(
                "cannot reject own request", code="forbidden", status=403
            )
        self._transition(
            request_id,
            "rejected",
            reject_reason=reason.strip(),
            expected=item["status"],
        )
        self._audit(
            principal,
            "index_request.reject",
            str(request_id),
            tenant_id=item["tenant_id"],
            request_id=request_id_header,
        )
        return self.get(request_id, principal, tenant_id=tenant_id)

    def _transition(
        self,
        request_id: int,
        target: str,
        *,
        expected: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Apply one legal transition with a database-level state guard."""
        from libref.db.conn import write_lock

        item = self._row(request_id)
        if item is None:
            raise IndexRequestError(
                f"unknown index request: {request_id}", status=404, code="not_found"
            )
        source = expected or item["status"]
        if source != item["status"]:
            raise IndexRequestError(
                f"stale index request {request_id}: expected {source}, found {item['status']}",
                code="conflict",
                status=409,
            )
        if source == target:
            return item
        if target not in _TRANSITIONS.get(source, frozenset()):
            raise IndexRequestError(
                f"cannot transition index request {source} -> {target}",
                code="conflict",
                status=409,
            )
        columns = ["status=?", f"updated_at={self._now_expr(self._conn)}"]
        values: list[Any] = [target]
        for key, value in fields.items():
            if key not in (
                "job_id",
                "error",
                "reject_reason",
                "approved_by",
            ):
                raise IndexRequestError(f"unsupported request field: {key}")
            columns.append(f"{key}=?")
            values.append(value)
        values.extend((request_id, source))
        with write_lock(self._conn):
            cur = self._conn.execute(
                f"UPDATE index_requests SET {', '.join(columns)} WHERE id=? AND status=?",
                tuple(values),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                raise IndexRequestError(
                    f"cannot transition index request {source} -> {target}",
                    code="conflict",
                    status=409,
                )
        return self._row(request_id) or item

    def start(self, request_id: int) -> bool:
        """Claim a queued request for execution; report whether the claim won."""
        try:
            self._transition(request_id, "running", expected="queued")
        except IndexRequestError as exc:
            if exc.code == "conflict":
                return False
            raise
        return True

    def finish(
        self,
        request_id: int,
        *,
        status: str,
        error: str = "",
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Write a terminal result from the job runner."""
        if status not in ("done", "failed", "cancelled"):
            raise IndexRequestError(f"unsupported terminal status: {status}")
        item = self._row(request_id)
        if item is None:
            raise IndexRequestError(
                f"unknown index request: {request_id}", status=404, code="not_found"
            )
        if item["status"] in TERMINAL_STATUSES:
            return item
        try:
            result = self._transition(
                request_id, status, error=error, expected=item["status"]
            )
        except IndexRequestError as exc:
            if exc.code != "conflict":
                raise
            current = self._row(request_id)
            if current is not None and current["status"] in TERMINAL_STATUSES:
                return current
            raise
        self._audit(
            None,
            "index_request.complete",
            str(request_id),
            tenant_id=tenant_id or result["tenant_id"],
            result=status,
        )
        return result

    def sync_job(self, job: Job | None) -> dict[str, Any] | None:
        """Reflect a job's state on its request when a runner did not write back."""
        if job is None:
            return None
        rid = _as_int(job.params.get("index_request_id"))
        if rid is None:
            return None
        item = self._row(rid)
        if item is None or item["status"] in TERMINAL_STATUSES:
            return item
        if job.status == "running":
            self.start(rid)
            return self._row(rid)
        if job.status == "done":
            return self.finish(rid, status="done")
        if job.status == "failed":
            status = "cancelled" if job.error == "cancelled" else "failed"
            return self.finish(rid, status=status, error=str(job.error or ""))
        return item

    async def wait(
        self,
        request_id: int,
        principal: Principal | None,
        *,
        tenant_id: str | None = None,
        timeout: float = 300.0,
        poll: float = 0.1,
    ) -> dict[str, Any]:
        """Bounded wait: return the current request on timeout, never block forever."""
        item = self.get(request_id, principal, tenant_id=tenant_id)
        deadline = time.monotonic() + max(0.0, float(timeout))
        if item["job_id"] is None:
            while item["status"] in ACTIVE_STATUSES and time.monotonic() < deadline:
                await asyncio.sleep(min(poll, max(0.0, deadline - time.monotonic())))
                item = self.get(request_id, principal, tenant_id=tenant_id)
            return item
        while item["status"] in ("pending", "approved", "queued", "running"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return item
            try:
                job = await self._jobs.wait_for(
                    int(item["job_id"]),
                    timeout=remaining,
                    poll=poll,
                )
            except TimeoutError:
                return self.get(request_id, principal, tenant_id=tenant_id)
            except KeyError:
                return self.get(request_id, principal, tenant_id=tenant_id)
            synced = self.sync_job(job)
            item = synced or self.get(request_id, principal, tenant_id=tenant_id)
            if job.status in ("done", "failed") and item["status"] in ACTIVE_STATUSES:
                settled = self.finish(
                    request_id,
                    status="done" if job.status == "done" else "failed",
                    error=str(job.error or ""),
                )
                item = settled
        return item
