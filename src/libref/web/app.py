"""Web routes: only parameter parsing plus service calls; all orchestration lives in the services layer."""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException

from libref.config import LibrefSettings, settings_fingerprint
from libref.crawler.registry import RegistryError
from libref.mcp_server.auth import verify_bearer
from libref.services.code.mining_read import MiningReadService
from libref.services.docs.ingest import IngestService
from libref.services.docs.pages import PageService
from libref.services.docs.search import SearchService
from libref.services.infra.jobs import JobManager
from libref.services.ops.review import ReviewService
from libref.services.registry.libraries import LibraryService
from libref.web.api import (
    AnswerQuery,
    ApiInsightQuery,
    JobQuery,
    PatternInsightQuery,
    ReadQuery,
    ResolveQuery,
    ReviewAction,
    ReviewQuery,
    ScrapeRequest,
    SearchQuery,
    envelope,
)


def _split_list(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    items = [part.strip() for part in raw.split(",")]
    items = [part for part in items if part]
    return items or None


def build_web_app(
    conn: sqlite3.Connection,
    settings: LibrefSettings,
    *,
    vec_store=None,
    embed=None,
    rerank=None,
    chat=None,
) -> FastAPI:
    """Assemble the web app; the caller manages the connection lifecycle."""
    from libref.services.registry.discovery import DiscoveryService

    svc = SearchService(conn, settings, vec_store=vec_store, embed=embed, rerank=rerank)
    discovery = DiscoveryService(conn, settings)
    pages = PageService(conn)
    catalog = LibraryService(conn)
    mining = MiningReadService(conn)
    review = ReviewService(conn)
    app = FastAPI(title="libref")

    def _guard(authorization: str | None = Header(default=None)) -> None:
        token = getattr(settings, "mcp_token", None)
        required = bool(getattr(settings, "mcp_require_auth", False))
        if token and not verify_bearer(authorization, token):
            raise HTTPException(status_code=401, detail="unauthorized")
        if required and not token:
            raise HTTPException(status_code=500, detail="auth misconfigured")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "fingerprint": settings_fingerprint(settings)}

    @app.get("/catalog")
    def catalog_route(
        library: str | None = None,
        ecosystem: str | None = None,
        _: None = Depends(_guard),
    ) -> dict[str, Any]:
        return envelope(catalog.catalog(library, ecosystem))

    @app.get("/search")
    async def search(
        query: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 8,
        mode: Literal["hybrid", "precise"] = "hybrid",
        expand_window: bool = False,
        window_radius: int | None = None,
        suggest: bool = False,
        _: None = Depends(_guard),
    ) -> dict[str, Any]:
        params = SearchQuery(
            query=query,
            library=library,
            version=version,
            ecosystem=ecosystem,
            limit=limit,
            mode=mode,
            expand_window=expand_window,
            window_radius=window_radius,
            suggest=suggest,
        )
        try:
            results, degraded = await svc.search_with_mode(
                params.query,
                params.mode,
                library=params.library,
                version=params.version,
                ecosystem=params.ecosystem,
                limit=params.limit,
                expand_window=params.expand_window or None,
                window_radius=params.window_radius,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        out = envelope(results, degraded=degraded)
        if params.suggest and not results:
            try:
                out["suggestions"] = await discovery.search_async(
                    params.library or params.query, params.ecosystem, params.limit
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RegistryError as exc:
                out["suggestions"] = []
                out["suggest_error"] = f"{exc.kind}: {exc}"
        return out

    @app.get("/library-search")
    async def library_search(
        query: str,
        ecosystem: str | None = None,
        limit: int = 10,
        _: None = Depends(_guard),
    ) -> dict[str, Any]:
        from libref.web.api import LibrarySearchQuery

        params = LibrarySearchQuery(query=query, ecosystem=ecosystem, limit=limit)
        try:
            items = await discovery.search_async(
                params.query, params.ecosystem, params.limit
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RegistryError as exc:
            raise HTTPException(status_code=502, detail=f"{exc.kind}: {exc}") from exc
        return envelope(items)

    @app.get("/answer")
    async def answer(
        query: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 5,
        expand_window: bool = False,
        window_radius: int | None = None,
        material_limit: int | None = None,
        _: None = Depends(_guard),
    ) -> dict[str, Any]:
        from libref.services.docs.answer import answer_query_async

        params = AnswerQuery(
            query=query,
            library=library,
            version=version,
            ecosystem=ecosystem,
            limit=limit,
            expand_window=expand_window,
            window_radius=window_radius,
            material_limit=material_limit,
        )
        try:
            return await answer_query_async(
                conn,
                settings,
                params.query,
                library=params.library,
                version=params.version,
                ecosystem=params.ecosystem,
                limit=params.limit,
                vec_store=vec_store,
                embed=embed,
                rerank=rerank,
                chat=chat,
                expand_window=params.expand_window or None,
                window_radius=params.window_radius,
                material_limit=params.material_limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/read")
    def read(
        url: str,
        mode: Literal["page", "window", "section"] = "page",
        sort_order: int | None = None,
        radius: int = 1,
        source_group_id: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        _: None = Depends(_guard),
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
        try:
            page = pages.read_doc(
                params.url,
                params.mode,
                sort_order=params.sort_order,
                radius=params.radius,
                source_group_id=params.source_group_id,
                library=params.library,
                version=params.version,
                ecosystem=params.ecosystem,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if page is None:
            raise HTTPException(status_code=404, detail="page not found")
        return page

    @app.get("/api-insight")
    def api_insight(
        view: Literal["usage", "related", "args", "common"],
        api: str | None = None,
        apis: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        _: None = Depends(_guard),
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
        try:
            return envelope(
                mining.api_insight(
                    params.view,
                    params.api,
                    apis=_split_list(params.apis),
                    library=params.library,
                    version=params.version,
                    ecosystem=params.ecosystem,
                    limit=params.limit,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/pattern-insight")
    def pattern_insight(
        view: Literal[
            "patterns",
            "evidence",
            "tree",
            "clusters",
            "health",
            "labels",
            "snippets",
        ],
        pattern: str | None = None,
        containing: str | None = None,
        prefix: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        per_file_limit: int | None = None,
        dedup: bool | None = None,
        _: None = Depends(_guard),
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
        )
        try:
            return envelope(
                mining.pattern_insight(
                    params.view,
                    pattern=_split_list(params.pattern),
                    containing=_split_list(params.containing),
                    prefix=params.prefix,
                    library=params.library,
                    version=params.version,
                    ecosystem=params.ecosystem,
                    limit=params.limit,
                    per_file_limit=params.per_file_limit,
                    dedup=params.dedup,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/review")
    def review_list(
        action: Literal["list"] = "list",
        limit: int = 50,
        _: None = Depends(_guard),
    ) -> dict[str, Any]:
        params = ReviewQuery(action=action, limit=limit)
        try:
            items = review.apply(params.action, None, params.limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return envelope(items)

    @app.post("/review")
    def review_apply(body: ReviewAction, _: None = Depends(_guard)) -> dict[str, Any]:
        try:
            result = review.apply(body.action, body.entity_id, body.limit)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if isinstance(result, list):
            return envelope(result)
        return result

    @app.post("/scrape")
    def scrape(body: ScrapeRequest, _: None = Depends(_guard)) -> dict[str, Any]:
        service = IngestService(settings, conn, vec_store=vec_store, embed=embed)
        counts = service.scrape_docs(
            body.library,
            body.version,
            body.start_url,
            max_pages=body.max_pages,
            ecosystem=body.ecosystem,
        )
        return {"counts": counts}

    @app.get("/jobs")
    def jobs(
        job_id: int | None = None,
        limit: int = 20,
        _: None = Depends(_guard),
    ) -> dict[str, Any]:
        params = JobQuery(job_id=job_id, limit=limit)
        mgr = JobManager(conn)
        out = mgr.show(params.job_id, params.limit)
        if "error" in out and "id" not in out:
            raise HTTPException(status_code=404, detail="job not found")
        return out

    @app.get("/jobs/{job_id}")
    def job_detail(job_id: int, _: None = Depends(_guard)) -> dict[str, Any]:
        mgr = JobManager(conn)
        out = mgr.show(job_id)
        if "error" in out and "id" not in out:
            raise HTTPException(status_code=404, detail="job not found")
        return out

    @app.get("/resolve")
    async def resolve_route(
        name: str,
        ecosystem: str | None = None,
        version: str | None = None,
        target: Literal["docs", "repo"] = "docs",
        _: None = Depends(_guard),
    ) -> dict[str, Any]:
        from libref.crawler.http import build_async_client
        from libref.services.registry.resolver import resolve as _resolve

        params = ResolveQuery(
            ecosystem=ecosystem, name=name, version=version, target=target
        )
        try:
            async with build_async_client(settings) as client:
                resolution = await _resolve(
                    conn,
                    client,
                    settings,
                    params.ecosystem,
                    params.name,
                    params.version,
                    params.target,
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RegistryError as exc:
            if exc.kind == "no_docs":
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return resolution.model_dump()

    return app
