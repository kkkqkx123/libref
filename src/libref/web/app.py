"""Web routes: only parameter parsing plus service calls; all orchestration lives in the services layer."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException

from libref.config import LibrefSettings, settings_fingerprint
from libref.mcp_server.auth import verify_bearer
from libref.services.ingest import IngestService
from libref.services.jobs import JobManager
from libref.services.libraries import LibraryService
from libref.services.mining_read import MiningReadService
from libref.services.pages import PageService
from libref.services.review import ReviewService
from libref.services.search import SearchService
from libref.web.api import (
    ReadPageQuery,
    ReadSectionQuery,
    ReadWindowQuery,
    ReviewAction,
    ScrapeRequest,
    SearchQuery,
    envelope,
)


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
    svc = SearchService(conn, settings, vec_store=vec_store, embed=embed, rerank=rerank)
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

    @app.get("/libraries")
    def libraries(_: None = Depends(_guard)) -> dict[str, Any]:
        return envelope(catalog.list_libraries())

    @app.get("/versions")
    def versions(
        library: str, ecosystem: str | None = None, _: None = Depends(_guard)
    ) -> dict[str, Any]:
        return envelope(catalog.list_versions(library, ecosystem))

    @app.get("/search")
    async def search(
        query: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 8,
        hybrid: bool = True,
        _: None = Depends(_guard),
    ) -> dict[str, Any]:
        params = SearchQuery(
            query=query, library=library, version=version,
            ecosystem=ecosystem, limit=limit, hybrid=hybrid,
        )
        if params.hybrid:
            results, degraded = await svc.search_hybrid_async(
                params.query, library=params.library, version=params.version,
                ecosystem=params.ecosystem, limit=params.limit,
            )
            return envelope(results, degraded=degraded)
        return envelope(
            svc.search_docs(
                params.query, library=params.library, version=params.version,
                ecosystem=params.ecosystem, limit=params.limit,
            )
        )

    @app.get("/read/page")
    def read_page(
        url: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        _: None = Depends(_guard),
    ) -> dict[str, Any]:
        params = ReadPageQuery(url=url, library=library, version=version, ecosystem=ecosystem)
        page = pages.read_page(
            params.url, library=params.library, version=params.version,
            ecosystem=params.ecosystem,
        )
        if page is None:
            raise HTTPException(status_code=404, detail="page not found")
        return page

    @app.get("/read/window")
    def read_window(
        url: str,
        sort_order: int,
        radius: int = 1,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        _: None = Depends(_guard),
    ) -> dict[str, Any]:
        params = ReadWindowQuery(
            url=url, sort_order=sort_order, radius=radius,
            library=library, version=version, ecosystem=ecosystem,
        )
        page = pages.read_window(
            params.url, params.sort_order, radius=params.radius,
            library=params.library, version=params.version, ecosystem=params.ecosystem,
        )
        if page is None:
            raise HTTPException(status_code=404, detail="window not found")
        return page

    @app.get("/read/section")
    def read_section(
        url: str,
        source_group_id: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        _: None = Depends(_guard),
    ) -> dict[str, Any]:
        params = ReadSectionQuery(
            url=url, source_group_id=source_group_id,
            library=library, version=version, ecosystem=ecosystem,
        )
        page = pages.read_section(
            params.url, params.source_group_id,
            library=params.library, version=params.version, ecosystem=params.ecosystem,
        )
        if page is None:
            raise HTTPException(status_code=404, detail="section not found")
        return page

    @app.get("/feature-tree")
    def feature_tree(
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        prefix: str | None = None,
        limit: int = 100,
        _: None = Depends(_guard),
    ) -> dict[str, Any]:
        return envelope(
            mining.feature_tree(
                library=library, version=version, ecosystem=ecosystem,
                prefix=prefix, limit=limit,
            )
        )

    @app.get("/clusters")
    def clusters(
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        containing: str | None = None,
        limit: int = 50,
        _: None = Depends(_guard),
    ) -> dict[str, Any]:
        names = containing.split(",") if containing else None
        return envelope(
            mining.api_clusters(
                library=library, version=version, ecosystem=ecosystem,
                containing=names, limit=limit,
            )
        )

    @app.get("/review/pending")
    def review_pending(limit: int = 50, _: None = Depends(_guard)) -> dict[str, Any]:
        return envelope(review.list_pending(limit=limit))

    @app.post("/review")
    def review_apply(body: ReviewAction, _: None = Depends(_guard)) -> dict[str, Any]:
        try:
            if body.action == "approve":
                review.approve(body.entity_id)
            elif body.action == "conflict":
                review.mark_conflict(body.entity_id)
            else:
                review.reopen(body.entity_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/scrape")
    def scrape(body: ScrapeRequest, _: None = Depends(_guard)) -> dict[str, Any]:
        service = IngestService(settings, conn, vec_store=vec_store, embed=embed)
        counts = service.scrape_docs(
            body.library, body.version, body.start_url,
            max_pages=body.max_pages, ecosystem=body.ecosystem,
        )
        return {"counts": counts}

    @app.get("/jobs")
    def jobs(limit: int = 20, _: None = Depends(_guard)) -> dict[str, Any]:
        mgr = JobManager(conn)
        return envelope([
            {"id": j.id, "kind": j.kind, "status": j.status,
             "finished_at": j.finished_at, "error": j.error}
            for j in mgr.list_recent(limit=limit)
        ])

    @app.get("/jobs/{job_id}")
    def job_detail(job_id: int, _: None = Depends(_guard)) -> dict[str, Any]:
        mgr = JobManager(conn)
        job = mgr.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {"id": job.id, "kind": job.kind, "status": job.status,
                "error": job.error, "result": job.result,
                "finished_at": job.finished_at}

    return app
