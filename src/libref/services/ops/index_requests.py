"""Approval-gated index requests: normalize targets, queue jobs, and close the loop.

The service owns the request state machine and is the only producer of indexing
jobs created through the request flow. SQL remains a thin persistence detail;
all exits call this orchestration layer instead of submitting those jobs.
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import json
import time
from typing import Any

from pydantic import BaseModel, Field

from libref.services.infra.auth import PLATFORM_SCOPE, Principal
from libref.services.infra.jobs import Job, JobManager
from libref.services.infra.tenant import owner_tenant

ACTIVE_STATUSES = frozenset({"pending", "approved", "queued", "running"})
TERMINAL_STATUSES = frozenset({"done", "failed", "rejected", "cancelled"})
INDEX_JOB_KINDS = frozenset({"scrape_docs", "index_repo"})

_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"approved", "rejected", "cancelled"}),
    "approved": frozenset({"queued", "cancelled", "pending"}),
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"done", "failed", "cancelled"}),
}


class IndexRequestItem(BaseModel):
    """One request row as exposed through HTTP, MCP, and CLI."""

    id: int
    tenant_id: str
    principal_id: int | None = None
    requester: str = ""
    kind: str
    target: str
    source: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    status: str
    dedupe_key: str = ""
    approved_by: int | None = None
    reject_reason: str = ""
    job_id: int | None = None
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


class IndexRequestError(Exception):
    """Business failure with a stable code for every exit."""

    def __init__(
        self, message: str, *, code: str = "invalid", status: int = 400
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class QuotaDenied(IndexRequestError):
    """Request frequency limit reached for the acting subject."""

    def __init__(self, resource: str, reset_at: str) -> None:
        super().__init__(
            f"quota exceeded: {resource} (reset at {reset_at})",
            code="quota_exceeded",
            status=429,
        )
        self.resource = resource
        self.reset_at = reset_at


def _json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except ValueError, TypeError:
        return default


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return str(value).strip() if value is not None else ""


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError, TypeError:
        raise IndexRequestError(f"{value!r} is not a valid id") from None


def _actor_id(principal: Principal | None) -> int | None:
    value = getattr(principal, "id", None)
    return int(value) if isinstance(value, int) else None


def _actor_name(principal: Principal | None) -> str:
    return str(getattr(principal, "name", "") or "")


def _has(principal: Principal | None, scope: str) -> bool:
    return bool(principal is not None and principal.has(scope))


def _validate_repo_source(source: str, settings: Any) -> str:
    """Accept only explicit HTTP(S) addresses; local paths stay CLI-direct only."""
    from urllib.parse import urlsplit

    from libref.crawler.registry import sanitize_http_url
    from libref.services.infra.tenant import validate_scrape_target

    text = source.strip().removeprefix("git+")
    try:
        scheme = (urlsplit(text).scheme or "").lower()
    except ValueError as exc:
        raise IndexRequestError(f"invalid repository address: {source}") from exc
    if scheme not in ("http", "https"):
        raise IndexRequestError(
            f"unsupported repository scheme: {scheme or '(missing)'}"
        )
    clean = sanitize_http_url(text, allow_private=False)
    if clean is None:
        raise IndexRequestError(f"invalid repository address: {source}")
    try:
        return validate_scrape_target(clean, settings)
    except ValueError as exc:
        raise IndexRequestError(str(exc)) from exc


def _validate_docs_source(start_url: str, settings: Any) -> str:
    from libref.services.infra.tenant import validate_scrape_target

    try:
        return validate_scrape_target(start_url, settings)
    except ValueError as exc:
        raise IndexRequestError(str(exc)) from exc


class IndexRequestService:
    """Normalize, de-duplicate, approve, execute, and read index requests."""

    TERMINAL_STATUSES = TERMINAL_STATUSES
    ACTIVE_STATUSES = ACTIVE_STATUSES

    def __init__(
        self, conn: Any, settings: Any, jobs: JobManager | None = None
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._jobs = jobs if jobs is not None else JobManager(conn)
        self._ensure_table()

    def _ensure_table(self) -> None:
        from libref.db.schema import INDEX_REQUESTS_SQL

        probe = getattr(self._conn, "diagnostics", None)
        if callable(probe):
            try:
                if str(probe().get("backend", "sqlite")) == "postgres":
                    return
            except Exception:  # noqa: BLE001, S110 — unreadable snapshot means embedded default
                pass
        self._conn.executescript(INDEX_REQUESTS_SQL)
        self._conn.commit()

    def _insert(self, sql: str, params: tuple[Any, ...]) -> int:
        inserter = getattr(self._conn, "insert", None)
        if callable(inserter):
            return int(inserter(sql, params))
        cur = self._conn.execute(sql, params)
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    @staticmethod
    def _now_expr(conn: Any) -> str:
        dialect = getattr(conn, "dialect", None)
        return str(dialect.now()) if dialect is not None else "datetime('now')"

    def _row(self, request_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT id, tenant_id, principal_id, requester, kind, target, source,"
            " params, status, dedupe_key, approved_by, reject_reason, job_id, error,"
            " created_at, updated_at FROM index_requests WHERE id=?",
            (request_id,),
        ).fetchone()
        return self._item(row) if row is not None else None

    @staticmethod
    def _item(row: tuple[Any, ...]) -> dict[str, Any]:
        return IndexRequestItem(
            id=int(row[0]),
            tenant_id=str(row[1]),
            principal_id=row[2],
            requester=str(row[3] or ""),
            kind=str(row[4]),
            target=str(row[5]),
            source=str(row[6] or ""),
            params=_json(row[7], {}),
            status=str(row[8]),
            dedupe_key=str(row[9] or ""),
            approved_by=row[10],
            reject_reason=str(row[11] or ""),
            job_id=row[12],
            error=str(row[13] or ""),
            created_at=str(row[14] or ""),
            updated_at=str(row[15] or ""),
        ).model_dump()

    def _visible(
        self, item: dict[str, Any], principal: Principal | None, tenant_id: str | None
    ) -> bool:
        if tenant_id not in (None, "*") and item["tenant_id"] != tenant_id:
            return False
        if _has(principal, "index:approve"):
            return True
        pid = _actor_id(principal)
        if pid is not None and item["principal_id"] is not None:
            return item["principal_id"] == pid
        return bool(item["requester"]) and item["requester"] == _actor_name(principal)

    def _owned(self, item: dict[str, Any], principal: Principal | None) -> bool:
        pid = _actor_id(principal)
        if pid is not None and item["principal_id"] is not None:
            return item["principal_id"] == pid
        return bool(item["requester"]) and item["requester"] == _actor_name(principal)

    def _audit(
        self,
        principal: Principal | None,
        action: str,
        target: str,
        *,
        tenant_id: str,
        result: str = "ok",
        request_id: str = "",
    ) -> None:
        from libref.services.ops.audit import write_audit

        write_audit(
            self._conn,
            tenant_id=tenant_id,
            principal_id=_actor_id(principal),
            action=action,
            target=target,
            result=result,
            request_id=request_id,
        )

    def _charge_quota(
        self, principal: Principal | None, tenant_id: str, request_id: str
    ) -> None:
        if not bool(getattr(self._settings, "quota_enabled", False)):
            return
        from libref.services.infra import quota

        decision = quota.enforce(
            self._conn,
            tenant_id=owner_tenant(tenant_id),
            principal_id=_actor_id(principal),
            resource="index_requests",
            settings=self._settings,
            request_id=request_id,
        )
        if decision is not None:
            raise QuotaDenied(decision.resource, decision.reset_at)

    async def _resolve_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Turn coordinates or explicit addresses into replayable job parameters."""
        from libref.crawler.http import build_async_client
        from libref.services.registry.resolver import resolve

        kind = _text(payload, "kind")
        library = _text(payload, "library")
        version = _text(payload, "version")
        ecosystem = _text(payload, "ecosystem") or "custom"
        include_prerelease = bool(payload.get("include_prerelease", False))
        if not library:
            raise IndexRequestError("library is required")
        if not version and kind not in ("repo", "docs"):
            raise IndexRequestError("version is required")

        if kind == "repo":
            source = _text(payload, "source")
            if source:
                if not version:
                    raise IndexRequestError(
                        "version is required for repository requests"
                    )
                address = _validate_repo_source(source, self._settings)
                resolved_from, confirmed = "explicit", True
            else:
                async with build_async_client(self._settings) as client:
                    try:
                        resolution = await resolve(
                            self._conn,
                            client,
                            self._settings,
                            ecosystem if ecosystem != "custom" else None,
                            library,
                            version or None,
                            "repo",
                            include_prerelease,
                        )
                    except ValueError as exc:
                        raise IndexRequestError(str(exc)) from exc
                if resolution.repo_url is None:
                    raise IndexRequestError("registry returned no repository address")
                address = _validate_repo_source(resolution.repo_url, self._settings)
                library, version = resolution.name, resolution.version
                ecosystem = resolution.ecosystem
                resolved_from = resolution.source
                confirmed = resolution.registry_confirmed
            params = {
                "library": library,
                "version": version,
                "source": address,
                "ecosystem": ecosystem,
                "update": bool(payload.get("update", False)),
                "fresh": bool(payload.get("fresh", False)),
                "resolved_from": resolved_from,
                "registry_confirmed": confirmed,
                "subdir": payload.get("subdir"),
                "only_files": payload.get("only_files"),
                "exclude_patterns": payload.get("exclude_patterns"),
                "max_files": payload.get("max_files"),
            }
            target = f"repo|{ecosystem}|{library}|{version}|{address}"
            return {"kind": kind, "source": address, "target": target, "params": params}

        start_url = _text(payload, "start_url")
        if start_url:
            if not version:
                raise IndexRequestError(
                    "version is required for documentation requests"
                )
            address = _validate_docs_source(start_url, self._settings)
            resolved_from, confirmed = "explicit", True
        else:
            async with build_async_client(self._settings) as client:
                try:
                    resolution = await resolve(
                        self._conn,
                        client,
                        self._settings,
                        ecosystem if ecosystem != "custom" else None,
                        library,
                        version or None,
                        "docs",
                        include_prerelease,
                    )
                except ValueError as exc:
                    raise IndexRequestError(str(exc)) from exc
            candidates = [
                link for link in [resolution.docs_url, *resolution.candidates] if link
            ]
            if not candidates:
                raise IndexRequestError("registry returned no documentation address")
            address = _validate_docs_source(candidates[0], self._settings)
            library, version = resolution.name, resolution.version
            ecosystem = resolution.ecosystem
            resolved_from = resolution.source
            confirmed = resolution.registry_confirmed
        params = {
            "library": library,
            "version": version,
            "start_url": address,
            "ecosystem": ecosystem,
            "max_pages": payload.get("max_pages"),
            "prefilter": bool(payload.get("prefilter", False)),
            "scope": payload.get("scope") or {},
            "resolved_from": resolved_from,
            "registry_confirmed": confirmed,
            "fresh": bool(payload.get("fresh", False)),
        }
        target = f"docs|{ecosystem}|{library}|{version}|{address}"
        return {"kind": kind, "source": address, "target": target, "params": params}

    @staticmethod
    def _dedupe_key(kind: str, target: str, params: dict[str, Any]) -> str:
        canonical = {
            "kind": kind,
            "target": target,
            "params": {k: params.get(k) for k in sorted(params)},
        }
        raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def submit(
        self,
        payload: dict[str, Any],
        principal: Principal | None,
        *,
        tenant_id: str | None = None,
        request_id: str = "",
    ) -> dict[str, Any]:
        """Create or reuse a request; privileged requesters enter the queue directly."""
        if not _has(principal, "index:request"):
            raise IndexRequestError(
                "missing scope: index:request", code="forbidden", status=403
            )
        kind = _text(payload, "kind")
        if kind not in ("repo", "docs"):
            raise IndexRequestError(f"unsupported request kind: {kind}")
        visibility = (
            tenant_id
            if tenant_id is not None
            else getattr(principal, "tenant_id", "default")
        )
        write_tenant = owner_tenant(visibility)
        normalized = await self._resolve_payload(payload)
        self._charge_quota(principal, write_tenant, request_id)
        dedupe = self._dedupe_key(kind, normalized["target"], normalized["params"])
        existing = self._conn.execute(
            "SELECT id FROM index_requests WHERE tenant_id=? AND dedupe_key=?"
            " AND status IN ('pending','approved','queued','running') ORDER BY id LIMIT 1",
            (write_tenant, dedupe),
        ).fetchone()
        if existing is not None:
            item = self._row(int(existing[0]))
            assert item is not None
            self._audit(
                principal,
                "index_request.create",
                str(item["id"]),
                tenant_id=write_tenant,
                result="reused",
                request_id=request_id,
            )
            return item

        from libref.db.conn import write_lock

        params = dict(normalized["params"])
        with write_lock(self._conn):
            new_id = self._insert(
                "INSERT INTO index_requests(tenant_id, principal_id, requester,"
                " kind, target, source, params, status, dedupe_key)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    write_tenant,
                    _actor_id(principal),
                    _actor_name(principal),
                    kind,
                    normalized["target"],
                    normalized["source"],
                    json.dumps(params, ensure_ascii=False),
                    "pending",
                    dedupe,
                ),
            )
            self._conn.commit()
        params["index_request_id"] = new_id
        self._conn.execute(
            "UPDATE index_requests SET params=? WHERE id=?",
            (json.dumps(params, ensure_ascii=False), new_id),
        )
        self._conn.commit()
        item = self._row(new_id)
        assert item is not None
        self._audit(
            principal,
            "index_request.create",
            str(new_id),
            tenant_id=write_tenant,
            request_id=request_id,
        )
        if _has(principal, "ingest:write"):
            self._audit(
                principal,
                "index_request.auto_approve",
                str(new_id),
                tenant_id=write_tenant,
                request_id=request_id,
            )
            item = self._queue(new_id, principal, tenant_id=write_tenant, approved=True)
        return item

    def _queue(
        self,
        request_id: int,
        principal: Principal | None,
        *,
        tenant_id: str,
        approved: bool,
    ) -> dict[str, Any]:
        """Submit the approved job and persist the association or a retryable error."""
        from libref.services.docs.scrape_jobs import start_scrape_job

        item = self._row(request_id)
        if item is None:
            raise IndexRequestError(
                f"unknown index request: {request_id}", status=404, code="not_found"
            )
        params = dict(item["params"])
        if approved and item["status"] == "pending":
            self._transition(
                request_id,
                "approved",
                approved_by=_actor_id(principal),
            )
        try:
            if item["kind"] == "docs":
                job_id = start_scrape_job(
                    self._jobs,
                    params["library"],
                    params["version"],
                    params["start_url"],
                    max_pages=params.get("max_pages"),
                    prefilter=bool(params.get("prefilter", False)),
                    scope=params.get("scope"),
                    ecosystem=str(params.get("ecosystem", "custom")),
                    resolved_from=params.get("resolved_from"),
                    registry_confirmed=bool(params.get("registry_confirmed", True)),
                    fresh=bool(params.get("fresh", False)),
                    index_request_id=request_id,
                    tenant_id=item["tenant_id"],
                    principal_id=item["principal_id"],
                )
            else:
                job_id = self._jobs.submit(
                    "index_repo",
                    params,
                    tenant_id=item["tenant_id"],
                    principal_id=item["principal_id"],
                )
        except Exception as exc:  # noqa: BLE001 — a failed queue keeps the request retryable
            self._transition(
                request_id,
                "pending",
                error=f"{type(exc).__name__}: {exc}",
                approved_by=_actor_id(principal) if approved else None,
            )
            return self._row(request_id) or item
        self._transition(
            request_id,
            "queued",
            job_id=job_id,
            error="",
            approved_by=_actor_id(principal) if approved else None,
        )
        return self._row(request_id) or item

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
        if not pending and not platform:
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
        """Cancel a pending request owned by the caller, including its queued job."""
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
            try:
                self._jobs.cancel(int(item["job_id"]), tenant_id=item["tenant_id"])
            except ValueError:
                pass
        self._transition(request_id, "cancelled")
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
        self._transition(request_id, "rejected", reject_reason=reason.strip())
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
        **fields: Any,
    ) -> dict[str, Any]:
        """Apply one legal state transition; illegal transitions raise."""
        from libref.db.conn import write_lock

        item = self._row(request_id)
        if item is None:
            raise IndexRequestError(
                f"unknown index request: {request_id}", status=404, code="not_found"
            )
        source = item["status"]
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
        values.append(request_id)
        with write_lock(self._conn):
            self._conn.execute(
                f"UPDATE index_requests SET {', '.join(columns)} WHERE id=?",
                tuple(values),
            )
            self._conn.commit()
        return self._row(request_id) or item

    def start(self, request_id: int) -> None:
        """Mark a queued request as running when its job is claimed."""
        try:
            self._transition(request_id, "running")
        except IndexRequestError as exc:
            if exc.status != 409:
                raise

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
        result = self._transition(request_id, status, error=error)
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


def build_index_request_runner(
    settings: Any, conn: Any, inner: Any, jobs: JobManager | None = None
):
    """Wrap a job runner so every request-backed job writes its terminal state."""
    svc = IndexRequestService(conn, settings, jobs=jobs)

    async def runner(job: Job):
        rid = (
            _as_int(job.params.get("index_request_id"))
            if isinstance(job.params, dict)
            else None
        )
        if rid is None:
            return await inner(job)
        svc.start(rid)
        try:
            produced = inner(job)
            if asyncio.iscoroutine(produced):
                produced = await produced
        except BaseException as exc:
            svc.finish(rid, status="failed", error=f"{type(exc).__name__}: {exc}")
            raise
        svc.finish(rid, status="done")
        return produced

    return runner
