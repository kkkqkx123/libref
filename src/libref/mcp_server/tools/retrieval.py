"""MCP tools for retrieval."""

from __future__ import annotations

from typing import Any, Literal

from libref.crawler.registry import RegistryError
from libref.limits import (
    ANSWER_MATERIALS,
    SEARCH_RESULTS,
    require_limit,
)
from libref.mcp_server.context import (
    _as_error,
    _invalid,
    _items,
    _not_found,
)
from libref.mcp_server.contract import ToolFailure
from libref.services.graph_query import (
    GRAPH_DIRECTIONS,
    GRAPH_MODES,
    GraphDirection,
    GraphMode,
)


def register(mcp, ctx, wrap) -> dict[str, object]:
    resolve_settings = ctx.settings
    discovery_svc = ctx.discovery_svc
    _principal = ctx.principal
    _tenant = ctx.tenant
    _make_bundle = ctx.make_bundle
    _close = ctx.close

    async def search(
        query: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 8,
        preset: Literal["precise", "balanced", "thorough"] = "balanced",
        suggest: bool = False,
        include_prerelease: bool = False,
        block_type: str | None = None,
        exclude_deprecated: bool = False,
        aligned_only: bool = False,
    ) -> dict[str, Any]:
        """Preset retrieval: precise uses exact full-text, balanced and thorough use fused recall."""
        if not query.strip():
            raise _invalid("query is empty", field="query")
        require_limit(limit, low=1, high=SEARCH_RESULTS)
        try:
            _rc, _bundle, _should_close = _make_bundle()
            try:
                results, status = await _bundle["search"].search_with_mode(
                    query,
                    preset,
                    library=library,
                    version=version,
                    ecosystem=ecosystem,
                    limit=limit,
                    tenant_id=_tenant(_principal()),
                    block_type=block_type,
                    exclude_deprecated=exclude_deprecated,
                    aligned_only=aligned_only,
                )
            finally:
                _close(_rc, _should_close)
        except Exception as exc:
            raise _as_error(exc) from exc
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
                raise _invalid(str(exc)) from exc
            except RegistryError as exc:
                # A failed suggestion pass must not discard the results already found.
                out["suggestions"] = []
                out["suggest_error"] = f"{exc.kind}: {exc}"
            except Exception as exc:
                raise _as_error(exc) from exc
        return out

    async def library_search(
        query: str,
        ecosystem: str | None = None,
        limit: int = 10,
        include_prerelease: bool = False,
    ) -> dict[str, Any]:
        """Fuzzy discovery across registries (pypi/npm/maven/go/crates/nuget; language aliases accepted)."""
        if not query.strip():
            raise _invalid("query is empty", field="query")
        require_limit(limit, low=1, high=SEARCH_RESULTS)
        try:
            return _items(
                await discovery_svc.search_async(
                    query, ecosystem, limit, include_prerelease=include_prerelease
                )
            )
        except Exception as exc:
            raise _as_error(exc) from exc

    async def web_search(
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Explicit web search for library docs or repos; results are unverified, confirm before use."""
        if not query.strip():
            raise _invalid("query is empty", field="query")
        require_limit(limit, low=1, high=SEARCH_RESULTS)
        try:
            return _items(await discovery_svc.search_web_async(query, limit))
        except Exception as exc:
            raise _as_error(exc) from exc

    async def github_search(
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Explicit hosted search for repos; results are unverified, confirm before use."""
        if not query.strip():
            raise _invalid("query is empty", field="query")
        require_limit(limit, low=1, high=SEARCH_RESULTS)
        try:
            return _items(await discovery_svc.search_github_async(query, limit))
        except Exception as exc:
            raise _as_error(exc) from exc

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
            raise _invalid("query is empty", field="query")
        require_limit(limit, low=1, high=ANSWER_MATERIALS)
        _rc2, _bundle2, _should_close2 = _make_bundle()
        try:
            try:
                return await answer_query_async(
                    _rc2,
                    _bundle2.get("settings", resolve_settings),
                    query,
                    library=library,
                    version=version,
                    ecosystem=ecosystem,
                    limit=limit,
                    vec_store=_bundle2.get("vec_store"),
                    embed=_bundle2.get("embed"),
                    rerank=_bundle2.get("rerank"),
                    chat=_bundle2.get("chat"),
                    preset=preset,
                    tenant_id=_tenant(_principal()),
                )
            except ValueError as exc:
                raise ToolFailure(
                    "unavailable",
                    str(exc),
                    hint="configure the chat model before asking for a generated answer",
                ) from exc
            except Exception as exc:
                raise _as_error(exc) from exc
        finally:
            _close(_rc2, _should_close2)

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
            raise _invalid("url is empty", field="url")
        if radius < 0 or radius > 20:
            raise _invalid("radius must be between 0 and 20", field="radius")
        if mode == "window" and sort_order is None:
            raise _invalid("window mode requires sort_order")
        if mode == "section" and not source_group_id:
            raise _invalid("section mode requires source_group_id")
        _rc3, _bundle3, _should_close3 = _make_bundle()
        try:
            try:
                page = _bundle3["pages"].read_doc(
                    url,
                    mode,
                    sort_order=sort_order,
                    radius=radius,
                    source_group_id=source_group_id,
                    library=library,
                    version=version,
                    ecosystem=ecosystem,
                    tenant_id=_tenant(_principal()),
                )
            except Exception as exc:
                raise _as_error(exc) from exc
        finally:
            _close(_rc3, _should_close3)
        if page is None:
            return _not_found(f"page not found: {url}")
        return page

    def catalog(
        library: str | None = None, ecosystem: str | None = None
    ) -> dict[str, Any]:
        """Browse the catalog: libraries when no name is given, versions otherwise."""
        _rc4, _bundle4, _should_close4 = _make_bundle()
        try:
            try:
                return _items(
                    _bundle4["catalog"].catalog(
                        library, ecosystem, tenant_id=_tenant(_principal())
                    )
                )
            finally:
                _close(_rc4, _should_close4)
        except Exception as exc:
            raise _as_error(exc) from exc

    def graph_query(
        mode: GraphMode = "neighbors",
        entity: str = "",
        target: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        profile: str | None = None,
        direction: GraphDirection = "both",
        depth: int | None = None,
        relation_types: list[str] | None = None,
        include_ambiguous: bool = False,
    ) -> dict[str, Any]:
        """Independent relation query over bounded, evidence-backed graph paths."""
        if mode not in GRAPH_MODES:
            raise _invalid(f"unsupported graph mode: {mode}", field="mode")
        if direction not in GRAPH_DIRECTIONS:
            raise _invalid(
                f"unsupported graph direction: {direction}", field="direction"
            )
        if not entity.strip():
            raise _invalid("entity is empty", field="entity")
        if mode == "path" and not str(target or "").strip():
            raise _invalid("path mode requires target", field="target")
        _rc5, _bundle5, _should_close5 = _make_bundle()
        try:
            try:
                return _bundle5["graph"].query(
                    mode,
                    entity,
                    target=target,
                    library=library,
                    version=version,
                    ecosystem=ecosystem,
                    tenant_id=_tenant(_principal()),
                    profile=profile,
                    direction=direction,
                    depth=depth,
                    relation_types=relation_types,
                    include_ambiguous=include_ambiguous,
                )
            except Exception as exc:
                raise _as_error(exc) from exc
        finally:
            _close(_rc5, _should_close5)

    return {
        "search": search,
        "graph_query": graph_query,
        "library_search": library_search,
        "web_search": web_search,
        "github_search": github_search,
        "answer": answer,
        "read_doc": read_doc,
        "catalog": catalog,
    }
