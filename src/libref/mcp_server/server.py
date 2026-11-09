"""MCP exit: MCPServer instance and tool registration (services only)."""

from __future__ import annotations

import sqlite3
from typing import Any

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
    search, gateway = build_search_service(conn, resolve_settings)
    pages = PageService(conn)
    catalog = LibraryService(conn)
    mining = MiningReadService(conn)
    review = ReviewService(conn)

    @mcp.tool()
    def search_docs(
        query: str,
        library: str | None = None,
        version: str | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Full-text search over the document library ordered by relevance."""
        return search.search_docs(query, library=library, version=version, limit=limit)

    @mcp.tool()
    def read_page(url: str, library: str | None = None, version: str | None = None) -> dict[str, Any] | None:
        """Read a full page reassembled from ordered blocks."""
        return pages.read_page(url, library=library, version=version)

    @mcp.tool()
    def read_window(url: str, sort_order: int, radius: int = 1) -> dict[str, Any] | None:
        """Read neighboring blocks around a hit for context expansion."""
        return pages.read_window(url, sort_order, radius)

    @mcp.tool()
    def read_section(url: str, source_group_id: str) -> dict[str, Any] | None:
        """Read a whole source group to trace a sub-block to its section."""
        return pages.read_section(url, source_group_id)

    @mcp.tool()
    def list_libraries() -> list[dict[str, Any]]:
        """List all libraries and their version counts."""
        return catalog.list_libraries()

    @mcp.tool()
    def list_versions(
        library: str, ecosystem: str | None = None
    ) -> list[dict[str, Any]]:
        """List all versions of a library with page counts."""
        return catalog.list_versions(library, ecosystem)

    @mcp.tool()
    def api_usage(
        library: str | None = None,
        version: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Count calls and covering scopes per API (frequency query)."""
        return mining.api_usage_counts(library=library, version=version, limit=limit)

    @mcp.tool()
    def related_apis(
        api: str,
        library: str | None = None,
        version: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Find APIs co-occurring in the same scopes (co-occurrence query)."""
        return mining.related_apis(api, library=library, version=version, limit=limit)

    @mcp.tool()
    def pattern_evidence(
        pattern: list[str],
        library: str | None = None,
        version: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Fetch source evidence for a call sequence pattern."""
        return mining.pattern_evidence(pattern, library=library, version=version, limit=limit)

    @mcp.tool()
    def feature_tree(
        library: str | None = None,
        version: str | None = None,
        prefix: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Browse hierarchical feature nodes by prefix."""
        return mining.feature_tree(library=library, version=version, prefix=prefix, limit=limit)

    @mcp.tool()
    def api_clusters(
        library: str | None = None,
        version: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Browse API clusters."""
        return mining.api_clusters(library=library, version=version, limit=limit)

    @mcp.tool()
    async def search_hybrid(
        query: str,
        library: str | None = None,
        version: str | None = None,
        limit: int = 8,
        window_radius: int | None = None,
        expand_window: bool = False,
    ) -> dict[str, Any]:
        """Hybrid retrieval matching CLI search --hybrid with degradation flag."""
        results, degraded = await search.search_hybrid_async(
            query, library=library, version=version, limit=limit,
            expand_window=expand_window or None, window_radius=window_radius,
        )
        return {"results": results, "degraded": degraded}

    @mcp.tool()
    async def answer(
        query: str,
        library: str | None = None,
        version: str | None = None,
        limit: int = 5,
        window_radius: int | None = None,
        expand_window: bool = False,
        material_limit: int | None = None,
    ) -> dict[str, Any]:
        """Hybrid retrieval plus grounded generation, with clear failure reasons."""
        from libref.services.answer import answer_query_async

        try:
            return await answer_query_async(
                conn, resolve_settings, query,
                library=library, version=version, limit=limit,
                expand_window=expand_window or None, window_radius=window_radius,
                material_limit=material_limit,
            )
        except ValueError as exc:
            return {"error": "unavailable", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"error": "failed", "message": str(exc)}

    @mcp.tool()
    def review_pending(limit: int = 50) -> list[dict[str, Any]]:
        """List pending call instances, same order as CLI review list."""
        return review.list_pending(limit=limit)

    @mcp.tool()
    def review_approve(entity_id: int) -> dict[str, Any]:
        """Move pending to approved, returning the reason on illegal moves."""
        try:
            review.approve(entity_id)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True}

    @mcp.tool()
    def review_conflict(entity_id: int) -> dict[str, Any]:
        """Mark pending as conflict, returning the reason on illegal moves."""
        try:
            review.mark_conflict(entity_id)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True}

    @mcp.tool()
    def review_reopen(entity_id: int) -> dict[str, Any]:
        """Reopen a conflict to pending, returning the reason when not in conflict."""
        try:
            review.reopen(entity_id)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True}

    @mcp.tool()
    def job_show(job_id: int | None = None, limit: int = 20) -> dict[str, Any]:
        """Show job results, listing recent jobs when no id is given."""
        from libref.services.jobs import JobManager

        mgr = JobManager(conn)
        if job_id is None:
            return {"jobs": [
                {"id": j.id, "kind": j.kind, "status": j.status,
                 "finished_at": j.finished_at, "error": j.error}
                for j in mgr.list_recent(limit=limit)
            ]}
        job = mgr.get(job_id)
        if job is None:
            return {"error": "not_found", "message": f"unknown job: {job_id}"}
        return {"id": job.id, "kind": job.kind, "status": job.status,
                "error": job.error, "result": job.result,
                "finished_at": job.finished_at}

    @mcp.tool()
    async def resolve_package(
        ecosystem: str | None,
        name: str,
        version: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a document source by ecosystem and name, read-only."""
        from libref.crawler.http import build_async_client

        try:
            async with build_async_client(resolve_settings) as client:
                resolution = await _resolve_package(
                    conn, client, resolve_settings, ecosystem, name, version,
                )
        except ValueError as exc:
            return {"error": "invalid", "message": str(exc)}
        except RegistryError as exc:
            return {"error": exc.kind, "message": str(exc), "hint": exc.hint}
        return resolution.model_dump()

    @mcp.tool()
    async def resolve_repo(
        ecosystem: str | None,
        name: str,
        version: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a repo address by ecosystem and name, read-only."""
        from libref.crawler.http import build_async_client
        from libref.services.resolver import resolve_repo as _resolve_repo

        try:
            async with build_async_client(resolve_settings) as client:
                resolution = await _resolve_repo(
                    conn, client, resolve_settings, ecosystem, name, version,
                )
        except ValueError as exc:
            return {"error": "invalid", "message": str(exc)}
        except RegistryError as exc:
            return {"error": exc.kind, "message": str(exc), "hint": exc.hint}
        return resolution.model_dump()

    return mcp
