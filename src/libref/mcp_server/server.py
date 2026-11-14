"""MCPServer instance and tool registration (services only)."""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from libref.config import ConfigError, LibrefSettings, load_settings
from libref.crawler.registry import RegistryError
from libref.observe import setup_logging
from libref.services.code.code_ingest import list_repo_dependencies
from libref.services.code.mining_read import MiningReadService
from libref.services.code.probe import probe_repo
from libref.services.docs.pages import PageService
from libref.services.docs.probe import probe_docs_async
from libref.services.factory import (
    build_discovery_service,
    build_gateway,
    build_search_service,
    build_vector,
)
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
        "api_insight",
        "pattern_insight",
        "symbol_insight",
        "call_insight",
        "import_insight",
        "mining_health",
        "review",
        "job_show",
        "resolve",
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


def build_mcp_server(
    conn: sqlite3.Connection, settings: LibrefSettings | None = None
) -> MCPServer:
    """Build the MCPServer instance and register only the visible tools."""
    resolve_settings = settings or load_settings()
    enabled = _effective_tools(resolve_settings)
    setup_logging(resolve_settings)
    mcp = MCPServer("libref")
    gateway = build_gateway(resolve_settings)
    vec_store, embed_fn, gateway = build_vector(resolve_settings, gateway)
    rerank_fn = gateway.rerank_fn()
    chat_fn = gateway.chat_fn()
    search_svc, _gateway = build_search_service(
        conn,
        resolve_settings,
        gateway=gateway,
        vec_store=vec_store,
        embed=embed_fn,
        rerank=rerank_fn,
    )
    discovery_svc = build_discovery_service(conn, resolve_settings)
    pages = PageService(conn)
    catalog_svc = LibraryService(conn)
    mining = MiningReadService(conn, resolve_settings)
    review_svc = ReviewService(conn)

    async def search(
        query: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 8,
        preset: Literal["precise", "balanced", "thorough"] = "balanced",
        suggest: bool = False,
        include_prerelease: bool = False,
    ) -> dict[str, Any]:
        """Preset retrieval: precise uses exact full-text, balanced and thorough use fused recall."""
        if not query.strip():
            return _invalid("query is empty")
        bad = _check_limit(limit, low=1, high=100)
        if bad is not None:
            return _invalid(bad)
        try:
            results, status = await search_svc.search_with_mode(
                query,
                preset,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit,
            )
        except ValueError as exc:
            return _invalid(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))
        out: dict[str, Any] = {
            "items": results,
            "count": len(results),
            "degraded": bool(status.get("degraded", False)),
            "reasons": list(status.get("reasons", []) or []),
            "answerable": bool(status["answerable"]),
            "answerability": float(status["answerability"]),
            "cross_lingual": bool(status["cross_lingual"]),
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

    async def web_search(
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Explicit web search for library docs or repos; results are unverified, confirm before use."""
        if not query.strip():
            return _invalid("query is empty")
        bad = _check_limit(limit, low=1, high=100)
        if bad is not None:
            return _invalid(bad)
        try:
            return _items(await discovery_svc.search_web_async(query, limit))
        except ValueError as exc:
            return _invalid(str(exc))
        except RegistryError as exc:
            return {"error": exc.kind, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))

    async def github_search(
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Explicit hosted search for repos; results are unverified, confirm before use."""
        if not query.strip():
            return _invalid("query is empty")
        bad = _check_limit(limit, low=1, high=100)
        if bad is not None:
            return _invalid(bad)
        try:
            return _items(await discovery_svc.search_github_async(query, limit))
        except ValueError as exc:
            return _invalid(str(exc))
        except RegistryError as exc:
            return {"error": exc.kind, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))

    async def answer(
        query: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 5,
        preset: Literal["precise", "balanced", "thorough"] = "balanced",
    ) -> dict[str, Any]:
        """Preset retrieval plus grounded generation, with clear failure reasons."""
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
                vec_store=vec_store,
                embed=embed_fn,
                rerank=rerank_fn,
                chat=chat_fn,
                preset=preset,
            )
        except ValueError as exc:
            return {"error": "unavailable", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"error": "failed", "message": str(exc)}

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

    async def doc_probe(
        start_url: str,
        sample_limit: int | None = None,
        scope: dict[str, Any] | None = None,
        fetch_sample_limit: int | None = None,
        max_pages: int | None = None,
        prefilter: bool = False,
    ) -> dict[str, Any]:
        """Probe a documentation site structure without storing; confirm the range before scraping."""
        if not start_url.strip():
            return _invalid("start_url is empty")
        bad = _check_limit(sample_limit, low=1, high=100)
        if bad is not None:
            return _invalid(bad)
        bad = _check_limit(fetch_sample_limit, low=0, high=100)
        if bad is not None:
            return _invalid(bad)
        bad = _check_limit(max_pages, low=1, high=100000)
        if bad is not None:
            return _invalid(bad)
        if not isinstance(prefilter, bool):
            return _invalid("prefilter must be a boolean")
        if scope is not None and not isinstance(scope, dict):
            return _invalid("scope must be an object")
        try:
            probe = await probe_docs_async(
                conn,
                resolve_settings,
                start_url,
                scope=scope,
                sample_limit=sample_limit,
                fetch_sample_limit=fetch_sample_limit,
                max_pages=max_pages,
                prefilter=prefilter,
            )
        except ValueError as exc:
            return _invalid(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))
        return probe.model_dump()

    def repo_probe(
        source: str,
        subdir: str | None = None,
        max_files: int | None = None,
        sample_limit: int | None = None,
        only_files: list[str] | None = None,
    ) -> dict[str, Any]:
        """Probe a repository structure without parsing; confirm the range before indexing."""
        if not source.strip():
            return _invalid("source is empty")
        bad = _check_limit(sample_limit, low=1, high=100)
        if bad is not None:
            return _invalid(bad)
        bad = _check_limit(max_files, low=1, high=100000)
        if bad is not None:
            return _invalid(bad)
        if only_files is not None and (
            not isinstance(only_files, list)
            or not only_files
            or any(not isinstance(item, str) or not item.strip() for item in only_files)
        ):
            return _invalid("only_files must be a non-empty string list")
        try:
            probe = probe_repo(
                source,
                settings=resolve_settings,
                subdir=subdir,
                max_files=max_files,
                sample_limit=sample_limit,
                only_files=only_files,
            )
        except ValueError as exc:
            return _invalid(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))
        return probe.model_dump()

    def repo_dependencies(
        library: str,
        version: str,
        ecosystem: str | None = None,
        dep_ecosystem: str | None = None,
    ) -> dict[str, Any]:
        """Manifest-declared dependencies for one library version, rebuilt at index time."""
        if not library.strip():
            return _invalid("library is empty")
        if not version.strip():
            return _invalid("version is empty")
        try:
            return _items(
                list_repo_dependencies(
                    conn,
                    library,
                    version,
                    dep_ecosystem,
                    ecosystem=ecosystem,
                )
            )
        except ValueError as exc:
            return _invalid(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))

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

    def symbol_insight(
        view: Literal["detail", "file", "children", "signature"],
        api: str | None = None,
        apis: list[str] | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Symbol detail views over version-scoped entities."""
        if view not in ("detail", "file", "children", "signature"):
            return _invalid(f"unsupported symbol view: {view}")
        if view == "detail" and not api and not apis:
            return _invalid("detail view requires api or apis")
        if view in ("file", "children", "signature") and not api:
            return _invalid(f"{view} view requires api")
        bad = _check_limit(limit, low=1, high=500)
        if bad is not None:
            return _invalid(bad)
        try:
            return _items(
                mining.symbol_insight(
                    view,
                    api,
                    apis=apis,
                    library=library,
                    version=version,
                    ecosystem=ecosystem,
                    limit=limit,
                    offset=offset,
                )
            )
        except ValueError as exc:
            return _invalid(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))

    def call_insight(
        view: Literal["callee", "caller", "file"],
        api: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        include_ambiguous: bool = False,
        resolve_kinds: list[str] | None = None,
        min_args: int | None = None,
        max_args: int | None = None,
    ) -> dict[str, Any]:
        """Bidirectional call views with shared mining filters."""
        if view not in ("callee", "caller", "file"):
            return _invalid(f"unsupported call view: {view}")
        if not api:
            return _invalid(f"{view} view requires api")
        bad = _check_limit(limit, low=1, high=500)
        if bad is not None:
            return _invalid(bad)
        try:
            return _items(
                mining.call_insight(
                    view,
                    api,
                    library=library,
                    version=version,
                    ecosystem=ecosystem,
                    limit=limit,
                    offset=offset,
                    include_ambiguous=include_ambiguous,
                    resolve_kinds=resolve_kinds,
                    min_args=min_args,
                    max_args=max_args,
                )
            )
        except ValueError as exc:
            return _invalid(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))

    def import_insight(
        view: Literal["importers", "imports"],
        api: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Import edge views for fan-in and fan-out."""
        if view not in ("importers", "imports"):
            return _invalid(f"unsupported import view: {view}")
        if not api:
            return _invalid(f"{view} view requires api")
        bad = _check_limit(limit, low=1, high=500)
        if bad is not None:
            return _invalid(bad)
        try:
            return _items(
                mining.import_insight(
                    view,
                    api,
                    library=library,
                    version=version,
                    ecosystem=ecosystem,
                    limit=limit,
                    offset=offset,
                )
            )
        except ValueError as exc:
            return _invalid(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _failed(str(exc))

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
            from libref.services.registry.resolver import next_actions_for

            hint = exc.hint or {}
            return {
                "error": exc.kind,
                "message": str(exc),
                "hint": hint,
                "next_actions": next_actions_for(
                    hint.get("ecosystem", ecosystem),
                    hint.get("name", name),
                    target,
                    exc.kind,
                ),
            }
        except Exception as exc:  # noqa: BLE001
            return {"error": "failed", "message": str(exc)}
        return resolution.model_dump()

    visible: dict[str, Any] = {
        "search": search,
        "library_search": library_search,
        "web_search": web_search,
        "github_search": github_search,
        "answer": answer,
        "read_doc": read_doc,
        "catalog": catalog,
        "doc_probe": doc_probe,
        "repo_probe": repo_probe,
        "repo_dependencies": repo_dependencies,
        "api_insight": api_insight,
        "pattern_insight": pattern_insight,
        "symbol_insight": symbol_insight,
        "call_insight": call_insight,
        "import_insight": import_insight,
        "mining_health": mining_health,
        "review": review,
        "job_show": job_show,
        "resolve": resolve,
    }
    for name, func in visible.items():
        if name in enabled:
            mcp.tool()(func)

    return mcp
