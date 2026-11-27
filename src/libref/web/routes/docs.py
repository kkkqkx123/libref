"""Web routes for docs."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request

from libref.crawler.registry import RegistryError
from libref.web.api import (
    AnswerQuery,
    AnswerResponse,
    DiscoveryResponse,
    GraphQuery,
    GraphResponse,
    LibraryResponse,
    PageList,
    ReadQuery,
    ReadResponse,
    SearchQuery,
    SearchResponse,
    VersionResponse,
    envelope,
)
from libref.web.errors import ApiRejection
from libref.web.request_context import (
    _guard,
    _read_guard,
    _tenant_of,
    close_rc,
)


def register(app, ctx) -> None:
    discovery = ctx.discovery
    _need = ctx.need
    _req_bundle = ctx.req_bundle

    @app.get("/catalog", response_model=LibraryResponse | VersionResponse)
    def catalog_route(
        request: Request,
        library: str | None = None,
        ecosystem: str | None = None,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            result = envelope(
                bundle["catalog"].catalog(
                    library, ecosystem, tenant_id=_tenant_of(principal)
                )
            )
            result["kind"] = "versions" if library else "libraries"
            return result
        finally:
            close_rc(rc, should_close)

    @app.get("/search", response_model=SearchResponse)
    async def search(
        request: Request,
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
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        params = SearchQuery(
            query=query,
            library=library,
            version=version,
            ecosystem=ecosystem,
            limit=limit,
            preset=preset,
            suggest=suggest,
            include_prerelease=include_prerelease,
            block_type=block_type,
            exclude_deprecated=exclude_deprecated,
            aligned_only=aligned_only,
        )
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                results, status = await bundle["search"].search_with_mode(
                    params.query,
                    params.preset,
                    library=params.library,
                    version=params.version,
                    ecosystem=params.ecosystem,
                    limit=params.limit,
                    tenant_id=_tenant_of(principal),
                    block_type=params.block_type,
                    exclude_deprecated=params.exclude_deprecated,
                    aligned_only=params.aligned_only,
                )
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
            out = envelope(results, degraded=bool(status.get("degraded", False)))
            out["reasons"] = list(status.get("reasons", []) or [])
            out["answerable"] = bool(status["answerable"])
            out["answerability"] = float(status["answerability"])
            out["cross_lingual"] = bool(status.get("cross_lingual", False))
            if params.suggest and not results:
                try:
                    from libref.services.registry.discovery import (
                        DiscoveryService as _DS,
                    )

                    _disc = _DS(rc, est)
                    out["suggestions"] = await _disc.search_async(
                        params.library or params.query,
                        params.ecosystem,
                        params.limit,
                        include_prerelease=params.include_prerelease,
                    )
                except ValueError as exc:
                    raise ApiRejection(400, "invalid_argument", str(exc)) from exc
                except RegistryError as exc:
                    out["suggestions"] = []
                    out["suggest_error"] = f"{exc.kind}: {exc}"
            return out
        finally:
            close_rc(rc, should_close)

    @app.get("/graph", response_model=GraphResponse)
    async def graph_query(
        request: Request,
        mode: Literal["neighbors", "path", "usage", "community", "impact"] = (
            "neighbors"
        ),
        entity: str = "",
        target: str | None = None,
        library: str = "",
        version: str = "",
        ecosystem: str | None = None,
        profile: str | None = None,
        direction: Literal["forward", "backward", "both"] = "both",
        depth: int | None = None,
        relation_types: Annotated[list[str] | None, Query()] = None,
        include_ambiguous: bool = False,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        if not entity.strip() or not library or not version:
            raise ApiRejection(
                400,
                "invalid_argument",
                "graph queries require entity, library and version",
            )
        params = GraphQuery(
            mode=mode,
            entity=entity,
            target=target,
            library=library,
            version=version,
            ecosystem=ecosystem,
            profile=profile,
            direction=direction,
            depth=depth,
            relation_types=relation_types,
            include_ambiguous=include_ambiguous,
        )
        _need(principal, "read")
        rc, should_close, _est, bundle = _req_bundle(request, principal)
        try:
            try:
                return bundle["graph"].query(
                    params.mode,
                    params.entity,
                    target=params.target,
                    library=params.library,
                    version=params.version,
                    ecosystem=params.ecosystem,
                    tenant_id=_tenant_of(principal),
                    profile=params.profile,
                    direction=params.direction,
                    depth=params.depth,
                    relation_types=params.relation_types,
                    include_ambiguous=params.include_ambiguous,
                )
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        finally:
            close_rc(rc, should_close)

    @app.get("/library-search", response_model=DiscoveryResponse)
    async def library_search(
        query: str,
        ecosystem: str | None = None,
        limit: int = 10,
        include_prerelease: bool = False,
        _: None = Depends(_read_guard),
    ) -> dict[str, Any]:
        from libref.web.api import LibrarySearchQuery

        params = LibrarySearchQuery(
            query=query,
            ecosystem=ecosystem,
            limit=limit,
            include_prerelease=include_prerelease,
        )
        try:
            items = await discovery.search_async(
                params.query,
                params.ecosystem,
                params.limit,
                include_prerelease=params.include_prerelease,
            )
        except ValueError as exc:
            raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        except RegistryError as exc:
            raise ApiRejection(
                502, "upstream_unavailable", f"{exc.kind}: {exc}"
            ) from exc
        return envelope(items)

    @app.get("/answer", response_model=AnswerResponse)
    async def answer(
        request: Request,
        query: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 5,
        preset: Literal["precise", "balanced", "thorough"] = "balanced",
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        from libref.llm.errors import LlmConfigError
        from libref.services.docs.answer import answer_query_async

        params = AnswerQuery(
            query=query,
            library=library,
            version=version,
            ecosystem=ecosystem,
            limit=limit,
            preset=preset,
        )
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return await answer_query_async(
                    rc,
                    est,
                    params.query,
                    library=params.library,
                    version=params.version,
                    ecosystem=params.ecosystem,
                    limit=params.limit,
                    vec_store=bundle.get("vec_store"),
                    embed=bundle.get("embed"),
                    rerank=bundle.get("rerank"),
                    chat=bundle.get("chat"),
                    preset=params.preset,
                    tenant_id=_tenant_of(principal),
                )
            except LlmConfigError as exc:
                # Config-level refusal: the UI phrases this as "chat not enabled"
                # instead of showing the raw English detail.
                raise ApiRejection(400, "chat_disabled", str(exc)) from exc
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        finally:
            close_rc(rc, should_close)

    @app.get("/read", response_model=ReadResponse)
    def read(
        request: Request,
        url: str,
        mode: Literal["page", "window", "section"] = "page",
        sort_order: int | None = None,
        radius: int = 1,
        source_group_id: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        params = ReadQuery(
            url=url,
            mode=mode,
            sort_order=sort_order,
            radius=radius,
            source_group_id=source_group_id,
            library=library,
            version=version,
            ecosystem=ecosystem,
        )
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                page = bundle["pages"].read_doc(
                    params.url,
                    params.mode,
                    sort_order=params.sort_order,
                    radius=params.radius,
                    source_group_id=params.source_group_id,
                    library=params.library,
                    version=params.version,
                    ecosystem=params.ecosystem,
                    tenant_id=_tenant_of(principal),
                )
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
            if page is None:
                raise ApiRejection(404, "page_not_found", "page not found")
            return page
        finally:
            close_rc(rc, should_close)

    @app.get("/pages", response_model=PageList)
    def pages_route(
        request: Request,
        library: str,
        version: str,
        ecosystem: str | None = None,
        limit: int = 50,
        offset: int = 0,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        """List page URLs under one library version; the reader is URL-addressed."""
        _need(principal, "read")
        if limit < 1 or limit > 200:
            raise ApiRejection(400, "invalid_limit", "limit must be between 1 and 200")
        if offset < 0:
            raise ApiRejection(400, "invalid_offset", "offset must be non-negative")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            return bundle["pages"].list_pages(
                library,
                version,
                ecosystem=ecosystem,
                limit=limit,
                offset=offset,
                tenant_id=_tenant_of(principal),
            )
        finally:
            close_rc(rc, should_close)
