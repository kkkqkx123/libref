"""MCPServer instance and tool registration (services only)."""

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
from libref.services.resolver import resolve as _resolve
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
        results, degraded = await search_svc.search_with_mode(
            query,
            mode,
            library=library,
            version=version,
            ecosystem=ecosystem,
            limit=limit,
            expand_window=expand_window or None,
            window_radius=window_radius,
        )
        return {"results": results, "degraded": degraded}

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
        return pages.read_doc(
            url,
            mode,
            sort_order=sort_order,
            radius=radius,
            source_group_id=source_group_id,
            library=library,
            version=version,
            ecosystem=ecosystem,
        )

    @mcp.tool()
    def catalog(
        library: str | None = None, ecosystem: str | None = None
    ) -> list[dict[str, Any]]:
        """Browse the catalog: libraries when no name is given, versions otherwise."""
        return catalog_svc.catalog(library, ecosystem)

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
        return mining.api_insight(
            view,
            api,
            library=library,
            version=version,
            ecosystem=ecosystem,
            limit=limit,
        )

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
        return mining.pattern_insight(
            view,
            pattern=pattern,
            containing=containing,
            prefix=prefix,
            library=library,
            version=version,
            ecosystem=ecosystem,
            limit=limit,
        )

    @mcp.tool()
    def review(
        action: Literal["list", "approve", "conflict", "reopen"],
        entity_id: int | None = None,
        limit: int = 50,
    ) -> Any:
        """Review workflow: list pending items or transition one item."""
        try:
            return review_svc.apply(action, entity_id, limit)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}

    @mcp.tool()
    def job_show(job_id: int | None = None, limit: int = 20) -> dict[str, Any]:
        """Show job results, listing recent jobs when no id is given."""
        from libref.services.jobs import JobManager

        return JobManager(conn).show(job_id, limit)

    @mcp.tool()
    async def resolve(
        ecosystem: str | None,
        name: str,
        version: str | None = None,
        target: Literal["docs", "repo"] = "docs",
    ) -> dict[str, Any]:
        """Resolve a package address by ecosystem and name, read-only."""
        from libref.crawler.http import build_async_client

        try:
            async with build_async_client(resolve_settings) as client:
                resolution = await _resolve(
                    conn,
                    client,
                    resolve_settings,
                    ecosystem,
                    name,
                    version,
                    target,
                )
        except ValueError as exc:
            return {"error": "invalid", "message": str(exc)}
        except RegistryError as exc:
            return {"error": exc.kind, "message": str(exc), "hint": exc.hint}
        return resolution.model_dump()

    return mcp
