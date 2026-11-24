"""Web routes for mining."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request

from libref.web.api import (
    ApiInsightQuery,
    ApiInsightResult,
    CallInsightQuery,
    CallInsightResult,
    DocInsightQuery,
    DocInsightResult,
    ImportInsightQuery,
    ImportInsightResult,
    MiningHealthResponse,
    PatternInsightQuery,
    PatternInsightResult,
    SymbolInsightQuery,
    SymbolInsightResult,
    insight_envelope,
)
from libref.web.errors import ApiRejection
from libref.web.request_context import (
    _guard,
    _tenant_of,
)


def register(app, ctx) -> None:
    _need = ctx.need
    _req_bundle = ctx.req_bundle

    @app.get("/api-insight", response_model=ApiInsightResult)
    def api_insight(
        request: Request,
        view: Literal["usage", "related", "args", "common"],
        api: str | None = None,
        apis: Annotated[list[str] | None, Query()] = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        params = ApiInsightQuery(
            view=view,
            api=api,
            apis=apis,
            library=library,
            version=version,
            ecosystem=ecosystem,
            limit=limit,
        )
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return insight_envelope(
                    view,
                    bundle["mining"].api_insight(
                        params.view,
                        params.api,
                        apis=params.apis,
                        library=params.library,
                        version=params.version,
                        ecosystem=params.ecosystem,
                        limit=params.limit,
                        tenant_id=_tenant_of(principal),
                    ),
                )
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/pattern-insight", response_model=PatternInsightResult)
    def pattern_insight(
        request: Request,
        view: Literal[
            "patterns",
            "evidence",
            "tree",
            "clusters",
            "health",
            "labels",
            "snippets",
            "facets",
            "feature_summary",
            "cluster_labels",
            "drift",
        ],
        pattern: Annotated[list[str] | None, Query()] = None,
        containing: Annotated[list[str] | None, Query()] = None,
        prefix: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        per_file_limit: int | None = None,
        dedup: bool | None = None,
        profile: str | None = None,
        facet: str | None = None,
        compare_profile: str | None = None,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        params = PatternInsightQuery(
            view=view,
            pattern=pattern,
            containing=containing,
            prefix=prefix,
            library=library,
            version=version,
            ecosystem=ecosystem,
            limit=limit,
            per_file_limit=per_file_limit,
            dedup=dedup,
            profile=profile,
            facet=facet,
            compare_profile=compare_profile,
        )
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return insight_envelope(
                    view,
                    bundle["mining"].pattern_insight(
                        params.view,
                        pattern=params.pattern,
                        containing=params.containing,
                        prefix=params.prefix,
                        library=params.library,
                        version=params.version,
                        ecosystem=params.ecosystem,
                        limit=params.limit,
                        per_file_limit=params.per_file_limit,
                        dedup=params.dedup,
                        profile=params.profile,
                        facet=params.facet,
                        compare_profile=params.compare_profile,
                        tenant_id=_tenant_of(principal),
                    ),
                )
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/doc-insight", response_model=DocInsightResult)
    def doc_insight(
        request: Request,
        view: Literal["features", "links", "coverage", "consistency"],
        library: str,
        version: str,
        ecosystem: str | None = None,
        profile: str | None = None,
        facet: str | None = None,
        api: str | None = None,
        pattern: str | None = None,
        verdict: str | None = None,
        block_type: str | None = None,
        document_id: int | None = None,
        limit: int = 50,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        params = DocInsightQuery(
            view=view,
            library=library,
            version=version,
            ecosystem=ecosystem,
            profile=profile,
            facet=facet,
            api=api,
            pattern=pattern,
            verdict=verdict,
            block_type=block_type,
            document_id=document_id,
            limit=limit,
        )
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return insight_envelope(
                    view,
                    bundle["doc_insight"].insight(
                        params.library,
                        params.version,
                        view=params.view,
                        ecosystem=params.ecosystem,
                        profile=params.profile,
                        facet=params.facet,
                        api=params.api,
                        pattern=params.pattern,
                        verdict=params.verdict,
                        block_type=params.block_type,
                        document_id=params.document_id,
                        limit=params.limit,
                        tenant_id=_tenant_of(principal),
                    ),
                )
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/symbol-insight", response_model=SymbolInsightResult)
    def symbol_insight(
        request: Request,
        view: Literal["detail", "file", "children", "signature"],
        api: str | None = None,
        apis: Annotated[list[str] | None, Query()] = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        params = SymbolInsightQuery(
            view=view,
            api=api,
            apis=apis,
            library=library,
            version=version,
            ecosystem=ecosystem,
            limit=limit,
            offset=offset,
        )
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return insight_envelope(
                    view,
                    bundle["mining"].symbol_insight(
                        params.view,
                        params.api,
                        apis=params.apis,
                        library=params.library,
                        version=params.version,
                        ecosystem=params.ecosystem,
                        limit=params.limit,
                        offset=params.offset,
                        tenant_id=_tenant_of(principal),
                    ),
                )
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/call-insight", response_model=CallInsightResult)
    def call_insight(
        request: Request,
        view: Literal["callee", "caller", "file"],
        api: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        include_ambiguous: bool = False,
        resolve_kinds: Annotated[list[str] | None, Query()] = None,
        relation_kinds: Annotated[list[str] | None, Query()] = None,
        min_args: int | None = None,
        max_args: int | None = None,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        params = CallInsightQuery(
            view=view,
            api=api,
            library=library,
            version=version,
            ecosystem=ecosystem,
            limit=limit,
            offset=offset,
            include_ambiguous=include_ambiguous,
            resolve_kinds=resolve_kinds,
            relation_kinds=relation_kinds,
            min_args=min_args,
            max_args=max_args,
        )
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return insight_envelope(
                    view,
                    bundle["mining"].call_insight(
                        params.view,
                        params.api,
                        library=params.library,
                        version=params.version,
                        ecosystem=params.ecosystem,
                        limit=params.limit,
                        offset=params.offset,
                        include_ambiguous=params.include_ambiguous,
                        resolve_kinds=params.resolve_kinds,
                        relation_kinds=params.relation_kinds,
                        min_args=params.min_args,
                        max_args=params.max_args,
                        tenant_id=_tenant_of(principal),
                    ),
                )
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/import-insight", response_model=ImportInsightResult)
    def import_insight(
        request: Request,
        view: Literal["importers", "imports"],
        api: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        params = ImportInsightQuery(
            view=view,
            api=api,
            library=library,
            version=version,
            ecosystem=ecosystem,
            limit=limit,
            offset=offset,
        )
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return insight_envelope(
                    view,
                    bundle["mining"].import_insight(
                        params.view,
                        params.api,
                        library=params.library,
                        version=params.version,
                        ecosystem=params.ecosystem,
                        limit=params.limit,
                        offset=params.offset,
                        tenant_id=_tenant_of(principal),
                    ),
                )
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/mining-health", response_model=MiningHealthResponse)
    def mining_health(
        request: Request,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return bundle["mining"].mining_health(
                    library=library,
                    version=version,
                    ecosystem=ecosystem,
                    tenant_id=_tenant_of(principal),
                )
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass
