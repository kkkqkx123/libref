"""MCP exit: MCPServer instance and tool registration (services only)."""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from libref.config import LibrefSettings, load_settings
from libref.crawler.registry import RegistryError
from libref.observe import setup_logging
from libref.services.factory import build_search_service
from libref.services.libraries import LibraryService
from libref.services.mining_read import MiningReadService
from libref.services.pages import PageService
from libref.services.resolver import resolve_package as _resolve_package
from libref.services.review import ReviewService


def build_mcp_server(
    conn: sqlite3.Connection, settings: LibrefSettings | None = None
) -> MCPServer:
    """Build the MCPServer instance and register document tools."""
    resolve_settings = settings or load_settings()
    setup_logging(resolve_settings)
    mcp = MCPServer("libref")
    search_svc, _gateway = build_search_service(conn, resolve_settings)
    pages = PageService(conn)
    catalog_svc = LibraryService(conn)
    mining = MiningReadService(conn)
    review_svc = ReviewService(conn)

    @mcp.tool()
    async def search(
        query: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 8,
        mode: Literal["hybrid", "precise"] = "hybrid",
        window_radius: int | None = None,
        expand_window: bool = False,
    ) -> dict[str, Any]:
        """Unified retrieval: hybrid fused recall by default, exact full-text when precise."""
        if mode == "precise":
            results = search_svc.search_docs(
                query,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit,
            )
            return {"results": results, "degraded": False}
        if mode == "hybrid":
            results, degraded = await search_svc.search_hybrid_async(
                query,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit,
                expand_window=expand_window or None,
                window_radius=window_radius,
            )
            return {"results": results, "degraded": degraded}
        raise ValueError(f"unsupported search mode: {mode}")

    @mcp.tool()
    async def answer(
        query: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 5,
        window_radius: int | None = None,
        expand_window: bool = False,
        material_limit: int | None = None,
    ) -> dict[str, Any]:
        """Hybrid retrieval plus grounded generation, with clear failure reasons."""
        from libref.services.answer import answer_query_async

        try:
            return await answer_query_async(
                conn,
                resolve_settings,
                query,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit,
                expand_window=expand_window or None,
                window_radius=window_radius,
                material_limit=material_limit,
            )
        except ValueError as exc:
            return {"error": "unavailable", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"error": "failed", "message": str(exc)}

    @mcp.tool()
    def read_doc(
        url: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        mode: Literal["page", "window", "section"] = "page",
        sort_order: int | None = None,
        radius: int = 1,
        source_group_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Read a document by URL: full page, neighboring window, or whole section."""
        if mode == "page":
            return pages.read_page(
                url, library=library, version=version, ecosystem=ecosystem
            )
        if mode == "window":
            if sort_order is None:
                raise ValueError("window mode requires sort_order")
            return pages.read_window(
                url,
                sort_order,
                radius,
                library=library,
                version=version,
                ecosystem=ecosystem,
            )
        if mode == "section":
            if not source_group_id:
                raise ValueError("section mode requires source_group_id")
            return pages.read_section(
                url,
                source_group_id,
                library=library,
                version=version,
                ecosystem=ecosystem,
            )
        raise ValueError(f"unsupported read mode: {mode}")

    @mcp.tool()
    def catalog(
        library: str | None = None, ecosystem: str | None = None
    ) -> list[dict[str, Any]]:
        """Browse the catalog: libraries when no name is given, versions otherwise."""
        if library is None:
            return catalog_svc.list_libraries()
        return catalog_svc.list_versions(library, ecosystem)

    @mcp.tool()
    def api_insight(
        view: Literal["usage", "related", "args"],
        api: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """API-centric usage: frequency ranking, co-occurring APIs, argument shapes."""
        if view == "usage":
            return mining.api_usage_counts(
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 20,
            )
        if view == "related":
            if not api:
                raise ValueError("related view requires api")
            return mining.related_apis(
                api,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 10,
            )
        if view == "args":
            if not api:
                raise ValueError("args view requires api")
            return mining.arg_shapes(
                api,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 10,
            )
        raise ValueError(f"unsupported api view: {view}")

    @mcp.tool()
    def pattern_insight(
        view: Literal["patterns", "evidence", "tree", "clusters"],
        pattern: list[str] | None = None,
        containing: list[str] | None = None,
        prefix: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Sequence and structure insight: patterns, evidence, feature tree, clusters."""
        if view == "patterns":
            return mining.list_patterns(
                library=library,
                version=version,
                ecosystem=ecosystem,
                containing=containing,
                limit=limit if limit is not None else 50,
            )
        if view == "evidence":
            if not pattern:
                raise ValueError("evidence view requires a non-empty pattern")
            return mining.pattern_evidence(
                pattern,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 5,
            )
        if view == "tree":
            return mining.feature_tree(
                library=library,
                version=version,
                ecosystem=ecosystem,
                prefix=prefix,
                limit=limit if limit is not None else 100,
            )
        if view == "clusters":
            return mining.api_clusters(
                library=library,
                version=version,
                ecosystem=ecosystem,
                containing=containing,
                limit=limit if limit is not None else 50,
            )
        raise ValueError(f"unsupported pattern view: {view}")

    @mcp.tool()
    def review(
        action: Literal["list", "approve", "conflict", "reopen"],
        entity_id: int | None = None,
        limit: int = 50,
    ) -> Any:
        """Review workflow: list pending items or transition one item."""
        if action == "list":
            return review_svc.list_pending(limit=limit)
        if entity_id is None:
            raise ValueError(f"{action} requires entity_id")
        try:
            if action == "approve":
                review_svc.approve(entity_id)
            elif action == "conflict":
                review_svc.mark_conflict(entity_id)
            elif action == "reopen":
                review_svc.reopen(entity_id)
            else:
                raise ValueError(f"unsupported review action: {action}")
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True}

    @mcp.tool()
    def job_show(job_id: int | None = None, limit: int = 20) -> dict[str, Any]:
        """Show job results, listing recent jobs when no id is given."""
        from libref.services.jobs import JobManager

        mgr = JobManager(conn)
        if job_id is None:
            return {
                "jobs": [
                    {
                        "id": j.id,
                        "kind": j.kind,
                        "status": j.status,
                        "finished_at": j.finished_at,
                        "error": j.error,
                    }
                    for j in mgr.list_recent(limit=limit)
                ]
            }
        job = mgr.get(job_id)
        if job is None:
            return {"error": "not_found", "message": f"unknown job: {job_id}"}
        return {
            "id": job.id,
            "kind": job.kind,
            "status": job.status,
            "error": job.error,
            "result": job.result,
            "finished_at": job.finished_at,
        }

    @mcp.tool()
    async def resolve(
        ecosystem: str | None,
        name: str,
        version: str | None = None,
        target: Literal["docs", "repo"] = "docs",
    ) -> dict[str, Any]:
        """Resolve a package address by ecosystem and name, read-only."""
        from libref.crawler.http import build_async_client
        from libref.services.resolver import resolve_repo as _resolve_repo

        try:
            async with build_async_client(resolve_settings) as client:
                if target == "docs":
                    resolution = await _resolve_package(
                        conn,
                        client,
                        resolve_settings,
                        ecosystem,
                        name,
                        version,
                    )
                elif target == "repo":
                    resolution = await _resolve_repo(
                        conn,
                        client,
                        resolve_settings,
                        ecosystem,
                        name,
                        version,
                    )
                else:
                    raise ValueError(f"unsupported resolve target: {target}")
        except ValueError as exc:
            return {"error": "invalid", "message": str(exc)}
        except RegistryError as exc:
            return {"error": exc.kind, "message": str(exc), "hint": exc.hint}
        return resolution.model_dump()

    return mcp
