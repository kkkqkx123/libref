"""MCP tools for index requests."""

from __future__ import annotations

from typing import Any, Literal

from libref.limits import (
    LISTING_ROWS,
    require_limit,
)
from libref.mcp_server.context import (
    _as_error,
    _invalid,
    _items,
)


def register(mcp, ctx, wrap) -> dict[str, object]:
    _principal = ctx.principal
    _require = ctx.require
    _tenant = ctx.tenant
    _make_bundle = ctx.make_bundle
    _close = ctx.close
    _request_service = ctx.request_service
    _with_next = ctx.with_next

    async def request_repo_index(
        library: str,
        version: str | None = None,
        ecosystem: str | None = None,
        source: str | None = None,
        include_prerelease: bool = False,
        update: bool = False,
        fresh: bool = False,
        subdir: str | None = None,
        only_files: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        max_files: int | None = None,
        source_role: str | None = None,
        target_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Request repository indexing by package coordinates or an HTTP(S) Git URL."""
        principal = _principal()
        _require(principal, "index:request")
        if not library.strip():
            raise _invalid("library is empty", field="library")
        payload = {
            "kind": "repo",
            "library": library,
            "version": version or "",
            "ecosystem": ecosystem,
            "source": source,
            "include_prerelease": include_prerelease,
            "update": update,
            "fresh": fresh,
            "subdir": subdir,
            "only_files": only_files,
            "exclude_patterns": exclude_patterns,
            "max_files": max_files,
            "source_role": source_role,
            "target_filter": target_filter,
        }
        rc, bundle, should_close = _make_bundle()
        try:
            try:
                item = await _request_service(rc, bundle).submit(
                    payload, principal, tenant_id=_tenant(principal)
                )
            except Exception as exc:
                raise _as_error(exc) from exc
            return _with_next(item)
        finally:
            _close(rc, should_close)

    async def request_docs_index(
        library: str,
        version: str | None = None,
        start_url: str | None = None,
        ecosystem: str | None = None,
        include_prerelease: bool = False,
        max_pages: int | None = None,
        prefilter: bool = False,
    ) -> dict[str, Any]:
        """Request documentation indexing by coordinates or an HTTP(S) start URL."""
        principal = _principal()
        _require(principal, "index:request")
        if not library.strip():
            raise _invalid("library is empty", field="library")
        payload = {
            "kind": "docs",
            "library": library,
            "version": version or "",
            "start_url": start_url,
            "ecosystem": ecosystem,
            "include_prerelease": include_prerelease,
            "max_pages": max_pages,
            "prefilter": prefilter,
        }
        rc, bundle, should_close = _make_bundle()
        try:
            try:
                item = await _request_service(rc, bundle).submit(
                    payload, principal, tenant_id=_tenant(principal)
                )
            except Exception as exc:
                raise _as_error(exc) from exc
            return _with_next(item)
        finally:
            _close(rc, should_close)

    def index_request_status(request_id: int) -> dict[str, Any]:
        """Read one index request: status, job id, error, and the next tool to call."""
        principal = _principal()
        _require(principal, "read")
        rc, bundle, should_close = _make_bundle()
        try:
            try:
                item = _request_service(rc, bundle).get(
                    request_id, principal, tenant_id=_tenant(principal)
                )
            except Exception as exc:
                raise _as_error(exc) from exc
            return _with_next(item)
        finally:
            _close(rc, should_close)

    async def wait_index_request(
        request_id: int, timeout: float = 300.0, poll: float = 0.2
    ) -> dict[str, Any]:
        """Poll one index request up to a bounded timeout and return its state."""
        principal = _principal()
        _require(principal, "read")
        if timeout < 0 or timeout > 900:
            raise _invalid("timeout must be between 0 and 900 seconds")
        if poll <= 0 or poll > 5:
            raise _invalid("poll must be between 0 and 5 seconds")
        rc, bundle, should_close = _make_bundle()
        try:
            try:
                item = await _request_service(rc, bundle).wait(
                    request_id,
                    principal,
                    tenant_id=_tenant(principal),
                    timeout=timeout,
                    poll=poll,
                )
            except Exception as exc:
                raise _as_error(exc) from exc
            result = _with_next(item)
            from libref.services.ops.index_requests import ACTIVE_STATUSES

            if item["status"] in ACTIVE_STATUSES:
                result["timed_out"] = True
            return result
        finally:
            _close(rc, should_close)

    def list_index_requests(
        status: str | None = None, pending: bool = False, limit: int = 50
    ) -> dict[str, Any]:
        """List your own index requests, or the pending queue when allowed."""
        principal = _principal()
        _require(principal, "index:request")
        require_limit(limit, low=1, high=LISTING_ROWS)
        rc, bundle, should_close = _make_bundle()
        try:
            try:
                items = _request_service(rc, bundle).list(
                    principal,
                    tenant_id=_tenant(principal),
                    status=status,
                    pending=pending,
                    limit=limit,
                )
            except Exception as exc:
                raise _as_error(exc) from exc
            return _items(items)
        finally:
            _close(rc, should_close)

    def review_index_request(
        action: Literal["list", "approve", "reject"],
        request_id: int | None = None,
        reason: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List pending index requests, approve one, or reject one with a reason."""
        principal = _principal()
        _require(principal, "index:approve")
        require_limit(limit, low=1, high=LISTING_ROWS)
        if action != "list" and request_id is None:
            raise _invalid(f"{action} requires request_id")
        if action == "reject" and not (reason or "").strip():
            raise _invalid("reject requires a reason")
        rc, bundle, should_close = _make_bundle()
        try:
            try:
                svc = _request_service(rc, bundle)
                if action == "list":
                    items = svc.list(
                        principal,
                        tenant_id=_tenant(principal),
                        pending=True,
                        limit=limit,
                    )
                    return _items(items)
                if action == "approve":
                    assert request_id is not None
                    item = svc.approve(
                        request_id, principal, tenant_id=_tenant(principal)
                    )
                else:
                    assert request_id is not None and reason is not None
                    item = svc.reject(
                        request_id, reason, principal, tenant_id=_tenant(principal)
                    )
                return _with_next(item)
            except Exception as exc:
                raise _as_error(exc) from exc
        finally:
            _close(rc, should_close)

    return {
        "request_repo_index": request_repo_index,
        "request_docs_index": request_docs_index,
        "index_request_status": index_request_status,
        "wait_index_request": wait_index_request,
        "list_index_requests": list_index_requests,
        "review_index_request": review_index_request,
    }
