"""MCPServer instance and tool registration (services only)."""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from libref.config import LibrefSettings, load_settings
from libref.crawler.registry import RegistryError
from libref.observe import setup_logging
from libref.services.code.mining_read import MiningReadService
from libref.services.docs.pages import PageService
from libref.services.factory import build_discovery_service, build_search_service
from libref.services.ops.review import ReviewService
from libref.services.registry.libraries import LibraryService
from libref.services.registry.resolver import resolve as _resolve


def _invalid(message: str) -> dict[str, Any]:
    return {"error": "invalid", "message": message}


def _not_found(message: str) -> dict[str, Any]:
    return {"error": "not_found", "message": message}


def _failed(message: str) -> dict[str, Any]:
    return {"error": "failed", "message": message}


def _items(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items, "count": len(items)}


def _check_limit(
    limit: int | None, *, low: int, high: int, name: str = "limit"
) -> str | None:
    if limit is None:
        return None
    if not isinstance(limit, int) or isinstance(limit, bool):
        return f"{name} must be an integer"
    if limit < low or limit > high:
        return f"{name} must be between {low} and {high}"
    return None


def build_mcp_server(
    conn: sqlite3.Connection, settings: LibrefSettings | None = None
) -> MCPServer:
    """Build the MCPServer instance and register document tools."""
    resolve_settings = settings or load_settings()
    setup_logging(resolve_settings)
    mcp = MCPServer("libref")
    search_svc, _gateway = build_search_service(conn, resolve_settings)
    discovery_svc = build_discovery_service(conn, resolve_settings)
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
        suggest: bool = False,
        include_prerelease: bool = False,
    ) -> dict[str, Any]:
        """Hybrid fused recall with degraded flag; precise uses exact full-text only."""
        if not query.strip():
            return _invalid("query is empty")
        bad = _check_limit(limit, low=1, high=100)
        if bad is not None:
            return _invalid(bad)
        if mode not in ("hybrid", "precise"):
            return _invalid(f"unsupported search mode: {mode}")
        if window_radius is not None and (
            not isinstance(window_radius, int)
            or isinstance(window_radius, bool)
            or window_radius < 0
            or window_radius > 20
        ):
            return _invalid("window_radius must be between 0 and 20")
        try:
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
        except ValueError as exc:
            return _invalid(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))
        out: dict[str, Any] = {
            "items": results,
            "count": len(results),
            "degraded": degraded,
        }
        if suggest and not results:
            try:
                out["suggestions"] = await discovery_svc.search_async(
                    library or query,
                    ecosystem,
                    limit,
                    include_prerelease=include_prerelease,
                )
            except ValueError as exc:
                return _invalid(str(exc))
            except RegistryError as exc:
                out["suggestions"] = []
                out["suggest_error"] = f"{exc.kind}: {exc}"
            except Exception as exc:  # noqa: BLE001
                return _failed(str(exc))
        return out

    @mcp.tool()
    async def library_search(
        query: str,
        ecosystem: str | None = None,
        limit: int = 10,
        include_prerelease: bool = False,
    ) -> dict[str, Any]:
        """Fuzzy discovery across registries (pypi/npm/maven/go/crates/nuget; language aliases accepted)."""
        if not query.strip():
            return _invalid("query is empty")
        bad = _check_limit(limit, low=1, high=100)
        if bad is not None:
            return _invalid(bad)
        try:
            return _items(
                await discovery_svc.search_async(
                    query, ecosystem, limit, include_prerelease=include_prerelease
                )
            )
        except ValueError as exc:
            return _invalid(str(exc))
        except RegistryError as exc:
            return {"error": exc.kind, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))

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
        from libref.services.docs.answer import answer_query_async

        if not query.strip():
            return _invalid("query is empty")
        bad = _check_limit(limit, low=1, high=50)
        if bad is not None:
            return _invalid(bad)
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
    ) -> dict[str, Any]:
        """Read a document by URL: full page, neighboring window, or whole section."""
        if not url.strip():
            return _invalid("url is empty")
        if mode not in ("page", "window", "section"):
            return _invalid(f"unsupported read mode: {mode}")
        if (
            not isinstance(radius, int)
            or isinstance(radius, bool)
            or radius < 0
            or radius > 20
        ):
            return _invalid("radius must be between 0 and 20")
        if mode == "window" and sort_order is None:
            return _invalid("window mode requires sort_order")
        if mode == "section" and not source_group_id:
            return _invalid("section mode requires source_group_id")
        try:
            page = pages.read_doc(
                url,
                mode,
                sort_order=sort_order,
                radius=radius,
                source_group_id=source_group_id,
                library=library,
                version=version,
                ecosystem=ecosystem,
            )
        except ValueError as exc:
            return _invalid(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))
        if page is None:
            return _not_found(f"page not found: {url}")
        return page

    @mcp.tool()
    def catalog(
        library: str | None = None, ecosystem: str | None = None
    ) -> dict[str, Any]:
        """Browse the catalog: libraries when no name is given, versions otherwise."""
        try:
            return _items(catalog_svc.catalog(library, ecosystem))
        except ValueError as exc:
            return _invalid(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))

    @mcp.tool()
    def api_insight(
        view: Literal["usage", "related", "args", "common"],
        api: str | None = None,
        apis: list[str] | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """API-centric usage: frequency ranking, co-occurring APIs, argument shapes."""
        if view not in ("usage", "related", "args", "common"):
            return _invalid(f"unsupported api view: {view}")
        if view in ("related", "args") and not api:
            return _invalid(f"{view} view requires api")
        if view == "common" and not api and not apis:
            return _invalid("common view requires api or apis")
        bad = _check_limit(limit, low=1, high=500)
        if bad is not None:
            return _invalid(bad)
        try:
            return _items(
                mining.api_insight(
                    view,
                    api,
                    apis=apis,
                    library=library,
                    version=version,
                    ecosystem=ecosystem,
                    limit=limit,
                )
            )
        except ValueError as exc:
            return _invalid(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))

    @mcp.tool()
    def pattern_insight(
        view: Literal[
            "patterns", "evidence", "tree", "clusters", "health", "labels", "snippets"
        ],
        pattern: list[str] | None = None,
        containing: list[str] | None = None,
        prefix: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        per_file_limit: int | None = None,
        dedup: bool | None = None,
    ) -> dict[str, Any]:
        """Sequence and structure insight: patterns, evidence, feature tree, clusters."""
        if view not in (
            "patterns",
            "evidence",
            "tree",
            "clusters",
            "health",
            "labels",
            "snippets",
        ):
            return _invalid(f"unsupported pattern view: {view}")
        if view == "evidence" and not pattern:
            return _invalid("evidence view requires a non-empty pattern")
        bad = _check_limit(limit, low=1, high=500)
        if bad is not None:
            return _invalid(bad)
        bad = _check_limit(per_file_limit, low=1, high=20)
        if bad is not None:
            return _invalid(bad)
        try:
            return _items(
                mining.pattern_insight(
                    view,
                    pattern=pattern,
                    containing=containing,
                    prefix=prefix,
                    library=library,
                    version=version,
                    ecosystem=ecosystem,
                    limit=limit,
                    per_file_limit=per_file_limit,
                    dedup=dedup,
                )
            )
        except ValueError as exc:
            return _invalid(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))

    @mcp.tool()
    def mining_health(
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
    ) -> dict[str, Any]:
        """Reuse and diversity health signals computed from derived tables."""
        try:
            return mining.mining_health(
                library=library, version=version, ecosystem=ecosystem
            )
        except ValueError as exc:
            return _invalid(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))

    @mcp.tool()
    def review(
        action: Literal["list", "approve", "conflict", "reopen"],
        entity_id: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Review workflow: list pending items or transition one item."""
        if action not in ("list", "approve", "conflict", "reopen"):
            return _invalid(f"unsupported review action: {action}")
        bad = _check_limit(limit, low=1, high=500)
        if bad is not None:
            return _invalid(bad)
        if action != "list" and entity_id is None:
            return _invalid(f"{action} requires entity_id")
        try:
            result = review_svc.apply(action, entity_id, limit)
        except ValueError as exc:
            text = str(exc)
            if "cannot transition" in text or "cannot reopen" in text:
                return {"error": "conflict", "message": text}
            return _invalid(text)
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))
        if isinstance(result, list):
            return _items(result)
        return result

    @mcp.tool()
    def job_show(job_id: int | None = None, limit: int = 20) -> dict[str, Any]:
        """Show job results, listing recent jobs when no id is given."""
        from libref.services.infra.jobs import JobManager

        bad = _check_limit(limit, low=1, high=200)
        if bad is not None:
            return _invalid(bad)
        try:
            return JobManager(conn).show(job_id, limit)
        except ValueError as exc:
            return _invalid(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))

    @mcp.tool()
    async def resolve(
        ecosystem: str | None,
        name: str,
        version: str | None = None,
        target: Literal["docs", "repo"] = "docs",
        include_prerelease: bool = False,
    ) -> dict[str, Any]:
        """Resolve a package address; fetches the registry and refreshes the cache."""
        from libref.crawler.http import build_async_client

        if not name.strip():
            return _invalid("empty package name")
        if target not in ("docs", "repo"):
            return _invalid(f"unsupported resolve target: {target}")
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
                    include_prerelease,
                )
        except ValueError as exc:
            return {"error": "invalid", "message": str(exc)}
        except RegistryError as exc:
            return {"error": exc.kind, "message": str(exc), "hint": exc.hint}
        except Exception as exc:  # noqa: BLE001
            return {"error": "failed", "message": str(exc)}
        return resolution.model_dump()

    return mcp
