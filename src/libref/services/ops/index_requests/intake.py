"""Request normalization, validation, de-duplication and submission."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import TYPE_CHECKING, Any

from libref.db.contract import DbIntegrityError
from libref.services.infra.auth import Principal
from libref.services.infra.tenant import owner_tenant
from libref.services.infra.timestamps import iso_utc
from libref.services.ops.index_requests.models import (
    IndexRequestError,
    IndexRequestItem,
    QuotaDenied,
    _actor_id,
    _actor_name,
    _has,
    _json,
    _target_filter,
    _text,
    _usage_role,
    _validate_docs_source,
    _validate_repo_source,
)


class Intake:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

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
            created_at=iso_utc(row[14]) or "",
            updated_at=iso_utc(row[15]) or "",
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
                "include_prerelease": include_prerelease,
                "subdir": payload.get("subdir"),
                "only_files": payload.get("only_files"),
                "exclude_patterns": payload.get("exclude_patterns"),
                "max_files": payload.get("max_files"),
                "source_role": _usage_role(payload.get("source_role")),
                "target_filter": _target_filter(payload.get("target_filter")),
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
            "include_prerelease": include_prerelease,
            "fresh": bool(payload.get("fresh", False)),
        }
        target = f"docs|{ecosystem}|{library}|{version}|{address}"
        return {"kind": kind, "source": address, "target": target, "params": params}

    def _active_by_dedupe(self, tenant_id: str, dedupe: str) -> int | None:
        row = self._conn.execute(
            "SELECT id FROM index_requests WHERE tenant_id=? AND dedupe_key=?"
            " AND status IN ('pending','approved','queued','running') ORDER BY id LIMIT 1",
            (tenant_id, dedupe),
        ).fetchone()
        return int(row[0]) if row is not None else None

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
        self._charge_quota(principal, write_tenant, request_id)
        normalized = await self._resolve_payload(payload)
        dedupe = self._dedupe_key(kind, normalized["target"], normalized["params"])

        from libref.db.conn import write_lock

        with write_lock(self._conn):
            reused_id = self._active_by_dedupe(write_tenant, dedupe)
            if reused_id is not None:
                item = self._row(reused_id)
                assert item is not None
                self._audit(
                    principal,
                    "index_request.create",
                    str(reused_id),
                    tenant_id=write_tenant,
                    result="reused",
                    request_id=request_id,
                )
                return item

            params = dict(normalized["params"])
            try:
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
                params["index_request_id"] = new_id
                self._conn.execute(
                    "UPDATE index_requests SET params=? WHERE id=?",
                    (json.dumps(params, ensure_ascii=False), new_id),
                )
                self._conn.commit()
            except DbIntegrityError, sqlite3.IntegrityError:
                self._conn.rollback()
                reused_id = self._active_by_dedupe(write_tenant, dedupe)
                if reused_id is None:
                    raise
                item = self._row(reused_id)
                assert item is not None
                self._audit(
                    principal,
                    "index_request.create",
                    str(reused_id),
                    tenant_id=write_tenant,
                    result="reused",
                    request_id=request_id,
                )
                return item
            except Exception:
                self._conn.rollback()
                raise

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
        if approved:
            if item["status"] != "pending":
                raise IndexRequestError(
                    f"cannot approve request in {item['status']}",
                    code="conflict",
                    status=409,
                )
            self._transition(
                request_id,
                "approved",
                approved_by=_actor_id(principal),
                expected="pending",
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
            try:
                self._transition(
                    request_id,
                    "pending",
                    error=f"{type(exc).__name__}: {exc}",
                    approved_by=_actor_id(principal) if approved else None,
                    expected="approved" if approved else item["status"],
                )
            except IndexRequestError as transition_error:
                if transition_error.code != "conflict":
                    raise
            return self._row(request_id) or item
        try:
            self._transition(
                request_id,
                "queued",
                job_id=job_id,
                error="",
                approved_by=_actor_id(principal) if approved else None,
                expected="approved" if approved else item["status"],
            )
        except IndexRequestError:
            try:
                self._jobs.cancel(job_id, tenant_id=item["tenant_id"])
            except ValueError, KeyError:
                pass
            raise
        return self._row(request_id) or item
