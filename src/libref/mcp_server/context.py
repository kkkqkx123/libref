"""Shared tool context and registration helpers for MCP tools."""

from __future__ import annotations

import inspect
import logging
from typing import Any

from libref.config import ConfigError, LibrefSettings
from libref.errors import LibrefError, normalize
from libref.mcp_server.contract import ToolFailure
from libref.mcp_server.contract import failed as as_tool_failure
from libref.services.infra.auth import (
    PLATFORM_SCOPE,
    missing_scopes,
    write_tenant,
)
from libref.services.infra.tenant import GLOBAL_TENANT

logger = logging.getLogger(__name__)


def _invalid(
    message: str, *, field: str | None = None, hint: str | None = None
) -> ToolFailure:
    """Caller mistake: the caller can correct it, so it is reported as a failure."""
    return ToolFailure("invalid_argument", message, field=field, hint=hint)


def _not_found(message: str) -> dict[str, Any]:
    """Absent resource: a normal result, so the caller keeps its own control flow."""
    return {"error": "not_found", "message": message}


def _items(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items, "count": len(items)}


def _as_error(exc: BaseException) -> ToolFailure:
    """Turn any service exception into the single value an exit can render."""
    found = normalize(exc)
    if found is not None:
        return as_tool_failure(found)
    logger.error("tool failure: %s", exc, exc_info=exc)
    return ToolFailure("unavailable", str(exc) or type(exc).__name__)


ALL_MCP_TOOLS: frozenset[str] = frozenset(
    {
        "search",
        "library_search",
        "web_search",
        "github_search",
        "answer",
        "read_doc",
        "catalog",
        "doc_probe",
        "repo_probe",
        "repo_dependencies",
        "link_stats",
        "api_insight",
        "pattern_insight",
        "doc_insight",
        "symbol_insight",
        "call_insight",
        "import_insight",
        "mining_health",
        "review",
        "job_show",
        "resolve",
        "submit_feedback",
        "request_repo_index",
        "request_docs_index",
        "index_request_status",
        "wait_index_request",
        "list_index_requests",
        "review_index_request",
    }
)


def _effective_tools(settings: LibrefSettings) -> set[str]:
    """Visible tool names after allowlist minus denylist; unknown names fail fast."""
    allowed = (
        set(settings.mcp_enabled_tools)
        if settings.mcp_enabled_tools is not None
        else set(ALL_MCP_TOOLS)
    )
    denied = set(settings.mcp_disabled_tools or [])
    unknown = (allowed | denied) - ALL_MCP_TOOLS
    if unknown:
        raise ConfigError(f"unknown MCP tools: {sorted(unknown)}")
    return allowed - denied


_READ_TOOLS = frozenset(
    {
        "search",
        "library_search",
        "web_search",
        "github_search",
        "answer",
        "read_doc",
        "catalog",
        "doc_probe",
        "repo_probe",
        "repo_dependencies",
        "link_stats",
        "api_insight",
        "pattern_insight",
        "doc_insight",
        "symbol_insight",
        "call_insight",
        "import_insight",
        "mining_health",
        "resolve",
        "job_show",
        "index_request_status",
        "wait_index_request",
    }
)

_QUOTA_TOOLS = frozenset({"answer", "web_search", "github_search", "resolve"})


class McpContext:
    """Builder-owned dependencies shared by MCP tool groups."""

    def __init__(
        self,
        conn,
        settings,
        context,
        search_svc,
        discovery_svc,
        pages,
        catalog_svc,
        mining,
        review_svc,
        gateway,
        vec_store,
        embed_fn,
        rerank_fn,
        chat_fn,
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.context = context
        self.search_svc = search_svc
        self.discovery_svc = discovery_svc
        self.pages = pages
        self.catalog_svc = catalog_svc
        self.mining = mining
        self.review_svc = review_svc
        self.gateway = gateway
        self.vec_store = vec_store
        self.embed_fn = embed_fn
        self.rerank_fn = rerank_fn
        self.chat_fn = chat_fn

    def principal(self):
        resolve_settings = self.settings
        from libref.services.infra.auth import current_principal, dev_principal

        found = current_principal.get()
        if found is not None:
            return found
        if (
            str(getattr(resolve_settings, "auth_mode", "enforced") or "enforced")
            == "dev"
        ):
            return dev_principal(resolve_settings)
        return None

    def require(self, principal, *scopes: str) -> None:
        conn = self.conn
        """Fail the call when credentials or scopes are missing."""
        if principal is None:
            raise ToolFailure(
                "unauthorized",
                "missing credentials",
                hint="send an Authorization: Bearer token",
            )
        missing = missing_scopes(principal, *scopes)
        if missing:
            from libref.services.ops.audit import write_audit

            write_audit(
                conn,
                tenant_id=write_tenant(principal),
                principal_id=getattr(principal, "id", None),
                action="auth.forbidden",
                target=",".join(missing),
                result="denied",
            )
            raise ToolFailure(
                "forbidden",
                f"missing {','.join(missing)}",
                hint="request the missing scope or use another account",
            )

    def tenant(self, principal) -> str:
        scopes = set(getattr(principal, "scopes", set()) or ())
        if PLATFORM_SCOPE in scopes:
            return GLOBAL_TENANT
        return str(getattr(principal, "tenant_id", "default") or "default")

    def make_bundle(self):
        conn = self.conn
        context = self.context
        search_svc = self.search_svc
        pages = self.pages
        catalog_svc = self.catalog_svc
        mining = self.mining
        review_svc = self.review_svc
        resolve_settings = self.settings
        vec_store = self.vec_store
        embed_fn = self.embed_fn
        rerank_fn = self.rerank_fn
        chat_fn = self.chat_fn
        gateway = self.gateway
        ectx = context
        # Prefer app-level context when set via middleware state? MCP has no request, use build-time context.
        if ectx is not None:
            rc = ectx.connect(check_same_thread=False)
            try:
                bundle = ectx.services_for(rc)
            except Exception:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass
                raise
            return rc, bundle, True
        from libref.services.docs.doc_insight import DocInsightService
        from libref.services.infra.jobs import JobManager
        from libref.services.ops.index_requests import IndexRequestService

        local_jobs = JobManager(conn)
        return (
            conn,
            {
                "search": search_svc,
                "pages": pages,
                "catalog": catalog_svc,
                "mining": mining,
                "doc_insight": DocInsightService(conn, resolve_settings),
                "review": review_svc,
                "jobs": local_jobs,
                "index_requests": IndexRequestService(
                    conn, resolve_settings, jobs=local_jobs
                ),
                "vec_store": vec_store,
                "embed": embed_fn,
                "rerank": rerank_fn,
                "chat": chat_fn,
                "gateway": gateway,
                "settings": resolve_settings,
            },
            False,
        )

    def close(self, rc, should_close: bool):
        if should_close:
            try:
                rc.close()
            except Exception:  # noqa: BLE001, S110
                pass

    def request_service(self, rc, bundle):
        resolve_settings = self.settings
        return bundle.get(
            "index_requests",
            __import__(
                "libref.services.ops.index_requests",
                fromlist=["IndexRequestService"],
            ).IndexRequestService(rc, resolve_settings),
        )

    def with_next(self, item: dict[str, Any]) -> dict[str, Any]:
        from libref.services.ops.index_requests import IndexRequestService

        result = dict(item)
        result["next_tools"] = IndexRequestService.next_tools(item)
        if item["status"] in ("pending", "approved", "queued", "running"):
            result["pending"] = True
        return result

    def read_tools(self) -> frozenset[str]:
        return _READ_TOOLS

    def quota_gate(self, name: str) -> None:
        """Reject an external-call tool when the caller is over quota."""
        resolve_settings = self.settings
        if not bool(getattr(resolve_settings, "quota_enabled", False)):
            return
        from libref.services.infra import quota as quota_mod

        principal = self.principal()
        rc, _bundle, should_close = self.make_bundle()
        try:
            decision = quota_mod.enforce(
                rc,
                tenant_id=self.tenant(principal),
                principal_id=getattr(principal, "id", None),
                resource="external_calls",
                cost=1,
                settings=resolve_settings,
            )
        finally:
            self.close(rc, should_close)
        if decision is not None:
            raise ToolFailure(
                "quota_exceeded",
                "quota exceeded for external calls",
                hint=f"limit {decision.limit_value} reached; resets at"
                f" {decision.reset_at}",
            )

    def wrap(self, name: str, func: Any) -> Any:
        _require = self.require
        _principal = self.principal
        _read_tools = self.read_tools()
        import functools as _ft

        # Only read tools need the scope guard; review/feedback do their own.
        check_scope = name in _read_tools and name not in (
            "review",
            "submit_feedback",
        )
        # Tools that trigger outbound network or LLM traffic are quota-gated
        # so MCP callers cannot bypass the limits enforced on the Web exit.
        quota_gated = name in _QUOTA_TOOLS

        if inspect.iscoroutinefunction(func):

            @_ft.wraps(func)
            async def _guarded(*args: Any, **kwargs: Any) -> Any:
                if check_scope:
                    _require(_principal(), "read")
                if quota_gated:
                    self.quota_gate(name)
                try:
                    return await func(*args, **kwargs)
                except LibrefError as exc:
                    # A shared error (e.g. require_limit) is not a ToolError, so
                    # the runner would turn it into a crash notice without this.
                    raise as_tool_failure(exc) from exc

            _guarded.__name__ = name
            return _guarded

        @_ft.wraps(func)
        def _guarded_sync(*args: Any, **kwargs: Any) -> Any:
            if check_scope:
                _require(_principal(), "read")
            if quota_gated:
                self.quota_gate(name)
            try:
                return func(*args, **kwargs)
            except LibrefError as exc:
                raise as_tool_failure(exc) from exc

        _guarded_sync.__name__ = name
        return _guarded_sync
