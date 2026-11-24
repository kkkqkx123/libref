"""Web routes: only parameter parsing plus service calls; all orchestration lives in the services layer."""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, HTTPException, Request

from libref.config import LibrefSettings
from libref.db.contract import MetadataSession
from libref.services.code.mining_read import MiningReadService
from libref.services.docs.pages import PageService
from libref.services.docs.search import SearchService
from libref.services.ops.review import ReviewService
from libref.services.registry.libraries import LibraryService
from libref.web.request_context import WebRequestContext


def _scope_list(raw: object) -> list[str]:
    try:
        parsed = json.loads(str(raw))
    except ValueError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def build_web_app(
    conn: MetadataSession,
    settings: LibrefSettings,
    *,
    vec_store=None,
    embed=None,
    rerank=None,
    chat=None,
    context=None,
) -> FastAPI:
    """Assemble the web app; per-request connections avoid single-conn blocking."""
    from libref.services.registry.discovery import DiscoveryService

    svc = SearchService(conn, settings, vec_store=vec_store, embed=embed, rerank=rerank)
    discovery = DiscoveryService(conn, settings)
    pages = PageService(conn)
    catalog = LibraryService(conn)
    mining = MiningReadService(conn, settings)
    review = ReviewService(conn)
    from contextlib import asynccontextmanager

    from libref.services.infra.runtime import check_config_fingerprint, drain

    @asynccontextmanager
    async def _lifespan(app):
        if context is not None:
            try:
                session = context.connect()
                check_config_fingerprint(session, settings)
            except Exception:
                logging.getLogger(__name__).debug(
                    "fingerprint check skipped", exc_info=True
                )
        yield
        try:
            session = context.connect() if context is not None else None
            manager = session and context.job_manager
            drain(manager, timeout=float(getattr(settings, "drain_timeout", 30.0)))
        except Exception:
            logging.getLogger(__name__).debug("drain on shutdown failed", exc_info=True)

    app = FastAPI(title="libref", lifespan=_lifespan)
    app.state.context = context
    app.state.settings = settings

    @app.exception_handler(HTTPException)
    async def _rejection(request: Request, exc: HTTPException):
        """Emit a stable ``error`` code next to the original ``detail`` text."""
        from fastapi.responses import JSONResponse

        from libref.web.errors import error_body, fallback_code

        rid = str(getattr(getattr(request, "state", None), "request_id", ""))
        headers = dict(exc.headers or {})
        if rid:
            headers.setdefault("x-request-id", rid)
        return JSONResponse(
            error_body(
                exc.status_code,
                exc.detail,
                rid,
                code=getattr(exc, "code", None) or fallback_code(exc.status_code),
            ),
            status_code=exc.status_code,
            headers=headers,
        )

    from libref.web.middleware import AuthMiddleware
    from libref.web.spa import SpaMiddleware, spa_dir_for

    app.add_middleware(AuthMiddleware, context=context, conn=conn, settings=settings)

    spa_directory = spa_dir_for(settings)
    if spa_directory is not None:
        assets = spa_directory / "assets"
        if assets.is_dir():
            from starlette.staticfiles import StaticFiles

            app.mount("/assets", StaticFiles(directory=assets), name="spa-assets")
        api_paths = {
            str(getattr(route, "path", ""))
            for route in app.routes
            if getattr(route, "path", "")
        }
        app.add_middleware(
            SpaMiddleware,
            directory=spa_directory,
            mcp_prefix=str(getattr(settings, "mcp_path", "/mcp") or "/mcp"),
            api_paths=api_paths,
        )
    elif bool(getattr(settings, "web_spa_enabled", True)) and str(
        getattr(settings, "web_spa_dir", "") or ""
    ):
        logging.getLogger(__name__).warning(
            "spa directory is missing index.html; running in pure-API mode"
        )

    ctx = WebRequestContext(
        conn,
        settings,
        context,
        svc,
        discovery,
        pages,
        catalog,
        mining,
        review,
        rerank,
        chat,
        vec_store,
        embed,
    )
    from libref.web.routes import admin as admin_routes
    from libref.web.routes import diagnostics as diagnostics_routes
    from libref.web.routes import discovery as discovery_routes
    from libref.web.routes import docs as docs_routes
    from libref.web.routes import feedback as feedback_routes
    from libref.web.routes import index_requests as index_request_routes
    from libref.web.routes import jobs as job_routes
    from libref.web.routes import mining as mining_routes
    from libref.web.routes import review as review_routes

    diagnostics_routes.register(app, ctx)
    docs_routes.register(app, ctx)
    mining_routes.register(app, ctx)
    review_routes.register(app, ctx)
    index_request_routes.register(app, ctx)
    discovery_routes.register(app, ctx)
    feedback_routes.register(app, ctx)
    job_routes.register(app, ctx)
    admin_routes.register(app, ctx)
    return app
