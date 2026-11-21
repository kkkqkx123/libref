"""Web routes: only parameter parsing plus service calls; all orchestration lives in the services layer."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request

from libref.config import LibrefSettings, settings_fingerprint
from libref.crawler.registry import RegistryError
from libref.db.contract import MetadataSession
from libref.errors import LibrefError
from libref.services.code.mining_read import MiningReadService
from libref.services.docs.pages import PageService
from libref.services.docs.search import SearchService
from libref.services.infra.auth import (
    PLATFORM_SCOPE,
    require_any_scope,
    require_scopes,
    write_tenant,
)
from libref.services.infra.jobs import JobManager
from libref.services.infra.tenant import GLOBAL_TENANT
from libref.services.infra.timestamps import epoch_utc, iso_utc
from libref.services.ops.review import ReviewService
from libref.services.registry.libraries import LibraryService
from libref.web.api import (
    AnswerQuery,
    AnswerResponse,
    ApiInsightQuery,
    ApiInsightResult,
    ApiKeyResponse,
    ApiValidationError,
    CallInsightQuery,
    CallInsightResult,
    ConfigRefreshResponse,
    DependencyResponse,
    DiscoveryResponse,
    DocProbeRequest,
    DocProbeResponse,
    ErrorResponse,
    FeedbackCreate,
    FeedbackResponse,
    FeedbackStatus,
    HealthResponse,
    ImportInsightQuery,
    ImportInsightResult,
    IndexRequestCreate,
    IndexRequestItem,
    IndexRequestReject,
    IndexRequestResponse,
    IndexRequestStatus,
    IssuedKey,
    JobCreate,
    JobDetail,
    JobListResponse,
    JobQuery,
    KeyCreate,
    LibraryResponse,
    LinkStatsQuery,
    MetricsResponse,
    MiningHealthResponse,
    OkIdResponse,
    OkResponse,
    PageLinkStats,
    PageList,
    PatternInsightQuery,
    PatternInsightResult,
    PrincipalListResponse,
    PrincipalResponse,
    QuotaPolicyResponse,
    QuotaPolicyUpsert,
    ReadQuery,
    ReadResponse,
    ReadyResponse,
    RepoProbeRequest,
    RepoProbeResponse,
    ResolutionResponse,
    ResolveQuery,
    ReviewAction,
    ReviewQuery,
    ReviewResponse,
    RevokedKeys,
    ScrapeRequest,
    SearchQuery,
    SearchResponse,
    SymbolInsightQuery,
    SymbolInsightResult,
    VersionLinkStats,
    VersionResponse,
    envelope,
    insight_envelope,
)
from libref.web.errors import ApiRejection, known_error_code


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

    def _guard(request: Request):
        principal = getattr(request.state, "principal", None)
        if principal is None:
            raise ApiRejection(401, "unauthorized", "unauthorized")
        return principal

    def _need(principal, *scopes: str) -> None:
        require_scopes(principal, *scopes)

    def _principal_id(principal) -> int | None:
        pid = getattr(principal, "id", None)
        return int(pid) if isinstance(pid, int) else None

    def _tenant_of(principal) -> str:
        scopes = set(getattr(principal, "scopes", set()) or ())
        if PLATFORM_SCOPE in scopes:
            return GLOBAL_TENANT
        return str(getattr(principal, "tenant_id", "default") or "default")

    def _write_tenant_of(principal) -> str:
        return write_tenant(principal)

    def _index_request_error(exc: Exception) -> HTTPException:
        """Translate service failures while preserving the stable error code."""
        from libref.services.ops.index_requests import IndexRequestError, QuotaDenied

        assert isinstance(exc, IndexRequestError)
        if isinstance(exc, QuotaDenied):
            detail = {
                "error": "quota exceeded",
                "detail": exc.message,
                "resource": exc.resource,
                "reset_at": exc.reset_at,
            }
            return ApiRejection(exc.status, known_error_code(exc.code), detail)
        return ApiRejection(exc.status, known_error_code(exc.code), exc.message)

    def _quota_gate(request: Request, principal, resource: str, cost: int = 1) -> None:
        """Reject the submission with 429 when the subject is over quota."""
        est = _eff_settings(request)
        if not bool(getattr(est, "quota_enabled", False)):
            return
        from libref.services.infra import quota as _quota

        rc, should_close, _, _bundle = _req_bundle(request, principal)
        try:
            decision = _quota.enforce(
                rc,
                tenant_id=_write_tenant_of(principal),
                principal_id=_principal_id(principal),
                resource=resource,
                cost=cost,
                settings=est,
                request_id=str(getattr(request.state, "request_id", "")),
            )
            if decision is not None:
                raise ApiRejection(
                    429,
                    "quota_exceeded",
                    {
                        "error": "quota exceeded",
                        "resource": resource,
                        "reset_at": decision.reset_at,
                        "request_id": str(getattr(request.state, "request_id", "")),
                    },
                )
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    def _eff_settings(request=None):
        try:
            st = getattr(
                getattr(getattr(request, "app", None), "state", None), "settings", None
            )
            if st is not None:
                return st
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            ctx = getattr(
                getattr(getattr(request, "app", None), "state", None), "context", None
            )
            if ctx is not None and getattr(ctx, "settings", None) is not None:
                return ctx.settings
        except Exception:  # noqa: BLE001, S110
            pass
        return settings

    def _eff_context(request=None):
        try:
            ctx = getattr(
                getattr(getattr(request, "app", None), "state", None), "context", None
            )
            if ctx is not None:
                return ctx
        except Exception:  # noqa: BLE001, S110
            pass
        return context

    def _req_bundle(request, principal):
        est = _eff_settings(request)
        ectx = _eff_context(request)
        if ectx is not None:
            rc = ectx.connect(check_same_thread=False)
            bundle = ectx.services_for(rc)
            # Override settings inside bundle with latest effective settings
            return rc, True, est, bundle
        # Fallback single-connection path for tests without context
        from libref.services.ops.index_requests import IndexRequestService

        jobs = JobManager(conn)
        fb = {
            "search": svc,
            "pages": pages,
            "catalog": catalog,
            "mining": mining,
            "review": review,
            "index_requests": IndexRequestService(conn, est, jobs=jobs),
            "jobs": jobs,
            "rerank": rerank,
            "chat": chat,
            "gateway": None,
            "vec_store": vec_store,
            "embed": embed,
            "settings": est,
        }
        return conn, False, est, fb

    def _read_guard(principal=Depends(_guard)):
        _need(principal, "read")

    @app.get("/health", response_model=HealthResponse)
    def health(request: Request) -> dict[str, Any]:
        return {"ok": True, "fingerprint": settings_fingerprint(_eff_settings(request))}

    @app.get("/me", response_model=PrincipalResponse)
    def me(principal=Depends(_guard)) -> dict[str, Any]:
        return {
            "id": _principal_id(principal),
            "name": str(getattr(principal, "name", "")),
            "kind": str(getattr(principal, "kind", "")),
            "tenant_id": _tenant_of(principal),
            "roles": list(getattr(principal, "roles", []) or []),
            "scopes": sorted(set(getattr(principal, "scopes", set()) or set())),
        }

    @app.get("/metrics", response_model=MetricsResponse)
    def metrics_route(principal=Depends(_guard)) -> dict[str, Any]:
        """Read-only process counters: requests, quota denials, token usage, latency."""
        _need(principal, "read")
        from libref.services.infra.metrics import snapshot as _snapshot

        snapshot = _snapshot()
        counters = {
            key: int(value)
            for key, value in snapshot.items()
            if key not in ("uptime_seconds", "duration_ms_p50", "duration_ms_p95")
        }
        return {
            "uptime_seconds": int(snapshot["uptime_seconds"]),
            "duration_ms_p50": snapshot.get("duration_ms_p50"),
            "duration_ms_p95": snapshot.get("duration_ms_p95"),
            "counters": counters,
        }

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
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

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
                )
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
            out = envelope(results, degraded=bool(status.get("degraded", False)))
            out["reasons"] = list(status.get("reasons", []) or [])
            out["answerable"] = bool(status["answerable"])
            out["answerability"] = float(status["answerability"])
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
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

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
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

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
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

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
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

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

    @app.get("/review", response_model=ReviewResponse)
    def review_list(
        request: Request,
        action: Literal["list"] = "list",
        limit: int = 50,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "review:read")
        params = ReviewQuery(action=action, limit=limit)
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                items = bundle["review"].apply(
                    params.action,
                    None,
                    params.limit,
                    tenant_id=_tenant_of(principal),
                )
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
            return envelope(items)
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.post("/review", response_model=OkResponse)
    def review_apply(
        body: ReviewAction, request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "review:write")
        from libref.services.ops.audit import write_audit

        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                bundle["review"].apply(
                    body.action,
                    body.entity_id,
                    body.limit,
                    tenant_id=_tenant_of(principal),
                    principal_id=_principal_id(principal),
                )
            except ValueError as exc:
                text = str(exc)
                if "cannot transition" in text or "cannot reopen" in text:
                    raise ApiRejection(409, "review_conflict", text) from exc
                raise ApiRejection(400, "invalid_argument", text) from exc
            write_audit(
                rc,
                tenant_id=_tenant_of(principal),
                principal_id=_principal_id(principal),
                action=f"review.{body.action}",
                target=str(body.entity_id or ""),
                request_id=str(
                    getattr(getattr(request, "state", None), "request_id", "")
                ),
            )
            return {"ok": True}
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.post("/index-requests", response_model=IndexRequestItem)
    async def index_request_create(
        body: IndexRequestCreate,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "index:request")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return await bundle["index_requests"].submit(
                    body.model_dump(exclude_none=True),
                    principal,
                    tenant_id=_tenant_of(principal),
                    request_id=str(getattr(request.state, "request_id", "")),
                )
            except Exception as exc:
                from libref.services.ops.index_requests import IndexRequestError

                if isinstance(exc, IndexRequestError):
                    raise _index_request_error(exc) from exc
                raise
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/index-requests", response_model=IndexRequestResponse)
    def index_request_list(
        request: Request,
        status: IndexRequestStatus | None = None,
        limit: int = 50,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "index:request")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                items = bundle["index_requests"].list(
                    principal,
                    tenant_id=_tenant_of(principal),
                    status=status,
                    limit=limit,
                )
            except Exception as exc:
                from libref.services.ops.index_requests import IndexRequestError

                if isinstance(exc, IndexRequestError):
                    raise _index_request_error(exc) from exc
                raise
            return envelope(items)
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/index-requests/pending", response_model=IndexRequestResponse)
    def index_request_pending(
        request: Request,
        limit: int = 50,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "index:approve")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            items = bundle["index_requests"].list(
                principal,
                tenant_id=_tenant_of(principal),
                pending=True,
                limit=limit,
            )
            return envelope(items)
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/index-requests/{request_id}", response_model=IndexRequestItem)
    def index_request_show(
        request_id: int,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return bundle["index_requests"].get(
                    request_id, principal, tenant_id=_tenant_of(principal)
                )
            except Exception as exc:
                from libref.services.ops.index_requests import IndexRequestError

                if isinstance(exc, IndexRequestError):
                    raise _index_request_error(exc) from exc
                raise
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.post("/index-requests/{request_id}/cancel", response_model=IndexRequestItem)
    def index_request_cancel(
        request_id: int,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "index:request")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return bundle["index_requests"].cancel(
                    request_id,
                    principal,
                    tenant_id=_tenant_of(principal),
                    request_id_header=str(getattr(request.state, "request_id", "")),
                )
            except Exception as exc:
                from libref.services.ops.index_requests import IndexRequestError

                if isinstance(exc, IndexRequestError):
                    raise _index_request_error(exc) from exc
                raise
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.post("/index-requests/{request_id}/approve", response_model=IndexRequestItem)
    def index_request_approve(
        request_id: int,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "index:approve")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return bundle["index_requests"].approve(
                    request_id,
                    principal,
                    tenant_id=_tenant_of(principal),
                    request_id_header=str(getattr(request.state, "request_id", "")),
                )
            except Exception as exc:
                from libref.services.ops.index_requests import IndexRequestError

                if isinstance(exc, IndexRequestError):
                    raise _index_request_error(exc) from exc
                raise
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.post("/index-requests/{request_id}/reject", response_model=IndexRequestItem)
    def index_request_reject(
        request_id: int,
        body: IndexRequestReject,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "index:approve")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return bundle["index_requests"].reject(
                    request_id,
                    body.reason,
                    principal,
                    tenant_id=_tenant_of(principal),
                    request_id_header=str(getattr(request.state, "request_id", "")),
                )
            except Exception as exc:
                from libref.services.ops.index_requests import IndexRequestError

                if isinstance(exc, IndexRequestError):
                    raise _index_request_error(exc) from exc
                raise
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.post("/scrape", response_model=OkIdResponse)
    def scrape(
        body: ScrapeRequest, request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "ingest:write")
        from libref.services.docs.scrape_jobs import start_scrape_job
        from libref.services.infra.tenant import validate_scrape_target
        from libref.services.ops.audit import write_audit

        rc, should_close, est, bundle = _req_bundle(request, principal)
        _quota_gate(request, principal, "job_submissions")
        try:
            try:
                validate_scrape_target(body.start_url, est)
            except ValueError as exc:
                write_audit(
                    rc,
                    tenant_id=_tenant_of(principal),
                    principal_id=_principal_id(principal),
                    action="scrape.reject",
                    target=f"{body.library}:{body.version}:{body.start_url}",
                    request_id=str(
                        getattr(getattr(request, "state", None), "request_id", "")
                    ),
                )
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
            mgr = bundle["jobs"]
            job_id = start_scrape_job(
                mgr,
                body.library,
                body.version,
                body.start_url,
                max_pages=body.max_pages,
                ecosystem=body.ecosystem,
                tenant_id=_write_tenant_of(principal),
                principal_id=_principal_id(principal),
            )
            write_audit(
                rc,
                tenant_id=_write_tenant_of(principal),
                principal_id=_principal_id(principal),
                action="scrape.submit",
                target=f"{body.library}:{body.version}:{job_id}",
                request_id=str(
                    getattr(getattr(request, "state", None), "request_id", "")
                ),
            )
            return {"id": job_id, "ok": True}
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.post("/doc-probe", response_model=DocProbeResponse)
    async def doc_probe(
        request: Request, body: DocProbeRequest, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "read")
        from libref.services.docs.probe import probe_docs_async
        from libref.services.infra.tenant import validate_scrape_target
        from libref.services.ops.audit import write_audit as _wa

        if body.scope is not None and not isinstance(body.scope, dict):
            raise ApiRejection(400, "invalid_scope", "scope must be an object")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                validate_scrape_target(body.start_url, est)
            except ValueError as exc:
                try:
                    _wa(
                        rc,
                        tenant_id=_tenant_of(principal),
                        principal_id=_principal_id(principal),
                        action="probe.reject",
                        target=body.start_url,
                        request_id=str(
                            getattr(getattr(request, "state", None), "request_id", "")
                        ),
                    )
                except Exception:  # noqa: BLE001, S110
                    pass
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
            try:
                probe = await probe_docs_async(
                    rc,
                    est,
                    body.start_url,
                    scope=body.scope,
                    sample_limit=body.sample_limit,
                    fetch_sample_limit=body.fetch_sample_limit,
                    max_pages=body.max_pages,
                    prefilter=body.prefilter,
                )
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
            return probe.model_dump()
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.post("/repo-probe", response_model=RepoProbeResponse)
    def repo_probe(
        request: Request, body: RepoProbeRequest, principal=Depends(_guard)
    ) -> dict[str, Any]:
        from libref.services.code.probe import probe_repo

        if body.only_files is not None and (
            not body.only_files or any(not item.strip() for item in body.only_files)
        ):
            raise ApiRejection(
                400, "invalid_argument", "only_files must be non-empty strings"
            )
        if body.exclude_patterns is not None and (
            not body.exclude_patterns
            or any(not item.strip() for item in body.exclude_patterns)
        ):
            raise ApiRejection(
                400, "invalid_argument", "exclude_patterns must be non-empty strings"
            )
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                probe = probe_repo(
                    body.source,
                    settings=est,
                    subdir=body.subdir,
                    max_files=body.max_files,
                    sample_limit=body.sample_limit,
                    only_files=body.only_files,
                    exclude_patterns=body.exclude_patterns,
                    tree_depth=body.tree_depth,
                    samples_per_dir=body.samples_per_dir,
                    allow_local=False,
                )
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
            return probe.model_dump()
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/jobs", response_model=JobListResponse)
    def jobs(
        request: Request,
        job_id: int | None = None,
        limit: int = 20,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "read")
        params = JobQuery(job_id=job_id, limit=limit)
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            mgr = bundle["jobs"]
            try:
                return mgr.show(
                    params.job_id, params.limit, tenant_id=_tenant_of(principal)
                )
            except LibrefError as exc:
                raise ApiRejection(404, "job_not_found", exc.message) from exc
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/jobs/{job_id}", response_model=JobDetail)
    def job_detail(
        request: Request, job_id: int, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            mgr = bundle["jobs"]
            try:
                return mgr.show(job_id, tenant_id=_tenant_of(principal))
            except LibrefError as exc:
                raise ApiRejection(404, "job_not_found", exc.message) from exc
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/resolve", response_model=ResolutionResponse)
    async def resolve_route(
        name: str,
        ecosystem: str | None = None,
        version: str | None = None,
        target: Literal["docs", "repo"] = "docs",
        include_prerelease: bool = False,
        _: None = Depends(_read_guard),
    ) -> dict[str, Any]:
        from libref.crawler.http import build_async_client
        from libref.services.registry.resolver import resolve as _resolve

        params = ResolveQuery(
            ecosystem=ecosystem,
            name=name,
            version=version,
            target=target,
            include_prerelease=include_prerelease,
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
                    params.include_prerelease,
                )
        except ValueError as exc:
            raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        except RegistryError as exc:
            if exc.kind in ("no_docs", "no_repo"):
                raise ApiRejection(404, "not_found", str(exc)) from exc
            raise ApiRejection(502, "upstream_unavailable", str(exc)) from exc
        return resolution.model_dump()

    @app.get("/web-search", response_model=DiscoveryResponse)
    async def web_search(
        query: str,
        limit: int = 10,
        _: None = Depends(_read_guard),
    ) -> dict[str, Any]:
        if not query.strip():
            raise ApiRejection(400, "invalid_argument", "query is empty")
        if limit < 1 or limit > 100:
            raise ApiRejection(400, "invalid_limit", "limit must be between 1 and 100")
        try:
            items = await discovery.search_web_async(query, limit)
        except ValueError as exc:
            raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        except RegistryError as exc:
            raise ApiRejection(
                502, "upstream_unavailable", f"{exc.kind}: {exc}"
            ) from exc
        return envelope(items)

    @app.get("/github-search", response_model=DiscoveryResponse)
    async def github_search(
        query: str,
        limit: int = 10,
        _: None = Depends(_read_guard),
    ) -> dict[str, Any]:
        if not query.strip():
            raise ApiRejection(400, "invalid_argument", "query is empty")
        if limit < 1 or limit > 100:
            raise ApiRejection(400, "invalid_limit", "limit must be between 1 and 100")
        try:
            items = await discovery.search_github_async(query, limit)
        except ValueError as exc:
            raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        except RegistryError as exc:
            raise ApiRejection(
                502, "upstream_unavailable", f"{exc.kind}: {exc}"
            ) from exc
        return envelope(items)

    @app.get("/repo-deps", response_model=DependencyResponse)
    def repo_deps(
        request: Request,
        library: str,
        version: str,
        ecosystem: str | None = None,
        dep_ecosystem: str | None = None,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        from libref.services.code.code_ingest import list_repo_dependencies

        if not library.strip():
            raise ApiRejection(400, "invalid_argument", "library is empty")
        if not version.strip():
            raise ApiRejection(400, "invalid_argument", "version is empty")
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                from libref.services.infra.tenant import is_global as _isg
                from libref.services.infra.tenant import resolve_version_id as _rvid
                from libref.services.infra.tenant import table_has_tenant as _htt

                _ten = _tenant_of(principal)
                if (
                    _ten not in (None, "*")
                    and not _isg(str(_ten))
                    and (_htt(rc, "libraries") or _htt(rc, "versions"))
                ):
                    try:
                        _rvid(rc, str(_ten), library, version, ecosystem)
                    except ValueError:
                        raise ApiRejection(
                            404, "page_not_found", "page not found"
                        ) from None
                rows = list_repo_dependencies(
                    rc,
                    library,
                    version,
                    dep_ecosystem,
                    ecosystem=ecosystem,
                    tenant_id=_ten,
                )
            except ValueError as exc:
                # preserve 404 for tenant-miss vs 400 for bad input
                if "unknown" in str(exc).lower() or "not found" in str(exc).lower():
                    raise ApiRejection(404, "not_found", str(exc)) from exc
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
            return envelope(rows)
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/link-stats", response_model=PageLinkStats | VersionLinkStats)
    def link_stats(
        request: Request,
        library: str,
        version: str,
        ecosystem: str | None = None,
        limit: int | None = None,
        url: str | None = None,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        from libref.services.docs.links import (
            page_link_stats,
            top_referenced,
            version_link_summary,
        )

        params = LinkStatsQuery(
            library=library,
            version=version,
            ecosystem=ecosystem,
            limit=limit,
            url=url,
        )
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                if params.url:
                    result = page_link_stats(
                        rc,
                        params.url,
                        params.library,
                        params.version,
                        params.ecosystem,
                        tenant_id=_tenant_of(principal),
                    )
                    result["kind"] = "page"
                    return result
                return {
                    "kind": "version",
                    "summary": version_link_summary(
                        rc,
                        params.library,
                        params.version,
                        params.ecosystem,
                        tenant_id=_tenant_of(principal),
                    ),
                    "items": top_referenced(
                        rc,
                        params.library,
                        params.version,
                        params.ecosystem,
                        limit=params.limit,
                        tenant_id=_tenant_of(principal),
                    ),
                }
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

    @app.get("/readyz", response_model=ReadyResponse)
    def readyz(request: Request) -> dict[str, Any]:
        est = _eff_settings(request)
        ectx = _eff_context(request)
        checks: dict[str, Any] = {}
        _rc = None
        try:
            if ectx is not None:
                _rc = ectx.connect(check_same_thread=False)
                _rc.execute("SELECT 1").fetchone()
            else:
                conn.execute("SELECT 1").fetchone()
            checks["sqlite"] = "ok"
        except Exception as exc:  # noqa: BLE001 — readiness reports degraded
            checks["sqlite"] = f"unavailable: {exc}"
        finally:
            if _rc is not None:
                try:
                    _rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass
        try:
            if ectx is not None:
                vec = ectx.vec_store
                emb = ectx.embed
                gw = ectx.gateway
            else:
                from libref.services.infra.vectors import build_vector as _build_vector

                vec, emb, gw = _build_vector(est)
            checks["vector"] = "disabled" if vec is None else "ok"
            checks["embed"] = "disabled" if emb is None else "ok"
            chat_fn = gw.chat_fn() if gw is not None else None
            checks["chat"] = "disabled" if chat_fn is None else "ok"
        except Exception as exc:  # noqa: BLE001 — vector optional
            checks["vector"] = f"unavailable: {exc}"
        degraded = any(v != "ok" and v != "disabled" for v in checks.values())
        out: dict[str, Any] = {
            "ok": not degraded,
            "checks": checks,
            "fingerprint": settings_fingerprint(est),
            "generation": getattr(ectx, "generation", 0) if ectx is not None else 0,
        }
        if degraded:
            out["degraded"] = True
        return out

    @app.post("/feedback", response_model=OkIdResponse)
    def feedback_create(
        body: FeedbackCreate,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "feedback:write")
        from libref.services.ops.audit import write_audit
        from libref.services.ops.feedback import FeedbackService

        rc, should_close, est, bundle = _req_bundle(request, principal)
        svc = FeedbackService(rc)
        try:
            fid = svc.submit(
                tenant_id=_write_tenant_of(principal),
                principal_id=_principal_id(principal),
                entity_type=body.entity_type,
                entity_id=body.entity_id,
                kind=body.kind,
                body=body.body,
            )
        except ValueError as exc:
            raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        write_audit(
            rc,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="feedback.create",
            target=str(fid),
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        if should_close:
            try:
                rc.close()
            except Exception:  # noqa: BLE001, S110
                pass
        return {"id": fid, "ok": True}

    @app.get("/feedback", response_model=FeedbackResponse)
    def feedback_list(
        request: Request,
        limit: int = 50,
        status: FeedbackStatus | None = None,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        scope = require_any_scope(principal, "feedback:read", "feedback:read:own")
        own_only = scope == "feedback:read:own"
        author_id = _principal_id(principal)
        if own_only and author_id is None:
            return envelope([])
        from libref.services.ops.feedback import FeedbackService

        rc, should_close, est, bundle = _req_bundle(request, principal)
        svc = FeedbackService(rc)
        items = svc.list_items(
            tenant_id=_tenant_of(principal),
            limit=max(1, min(limit, 200)),
            status=status,
            principal_id=author_id if own_only else None,
        )
        return envelope(items)

    @app.post("/feedback/{feedback_id}/resolve", response_model=OkResponse)
    def feedback_resolve(
        feedback_id: int,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "feedback:resolve")
        from libref.services.ops.audit import write_audit
        from libref.services.ops.feedback import FeedbackService

        rc, should_close, est, bundle = _req_bundle(request, principal)
        svc = FeedbackService(rc)
        try:
            svc.resolve(
                feedback_id,
                resolved_by=_principal_id(principal),
                tenant_id=_tenant_of(principal),
            )
        except ValueError as exc:
            raise ApiRejection(404, "not_found", str(exc)) from exc
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="feedback.resolve",
            target=str(feedback_id),
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        return {"ok": True}

    @app.post("/jobs", response_model=OkIdResponse)
    def jobs_create(
        body: JobCreate,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "jobs:write")
        _quota_gate(request, principal, "job_submissions")
        from libref.services.ops.audit import write_audit

        rc, should_close, est, bundle = _req_bundle(request, principal)
        mgr = bundle["jobs"]
        job_id = mgr.submit(
            body.kind,
            body.params.model_dump(exclude_none=True),
            tenant_id=_write_tenant_of(principal),
            principal_id=_principal_id(principal),
        )
        write_audit(
            rc,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="jobs.create",
            target=f"{body.kind}:{job_id}",
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        if should_close:
            try:
                rc.close()
            except Exception:  # noqa: BLE001, S110
                pass
        return {"id": job_id, "ok": True}

    @app.post("/jobs/{job_id}/cancel", response_model=OkResponse)
    def jobs_cancel(
        job_id: int,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "jobs:write")
        from libref.services.ops.audit import write_audit
        from libref.services.ops.index_requests import INDEX_JOB_KINDS

        rc, should_close, est, bundle = _req_bundle(request, principal)
        mgr = bundle["jobs"]
        try:
            job = mgr.get(job_id)
            if job is not None and job.kind in INDEX_JOB_KINDS:
                raise ApiRejection(
                    400,
                    "invalid_argument",
                    "indexing jobs must be cancelled through index requests",
                )
            mgr.cancel(job_id, tenant_id=_tenant_of(principal))
        except ValueError as exc:
            raise ApiRejection(404, "not_found", str(exc)) from exc
        write_audit(
            rc,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="jobs.cancel",
            target=str(job_id),
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        if should_close:
            try:
                rc.close()
            except Exception:  # noqa: BLE001, S110
                pass
        return {"ok": True}

    @app.get("/admin/principals", response_model=PrincipalListResponse)
    def admin_principals_list(
        request: Request,
        tenant_id: str | None = None,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        """Read-only principal view; role conflicts are flagged, never repaired."""
        _need(principal, "admin:keys")
        from libref.services.infra.auth import list_principals
        from libref.services.ops.audit import write_audit

        rows = list_principals(conn, viewer=principal, tenant_id=tenant_id)
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="principals.list",
            target=str(len(rows)),
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        return envelope([dict(row) for row in rows])

    @app.get("/admin/keys", response_model=ApiKeyResponse)
    def admin_keys_list(request: Request, principal=Depends(_guard)) -> dict[str, Any]:
        _need(principal, "admin:keys")
        from libref.services.infra.auth import PLATFORM_SCOPE
        from libref.services.ops.audit import write_audit

        global_view = PLATFORM_SCOPE in set(getattr(principal, "scopes", set()))
        sql = (
            "SELECT k.id, k.principal_id, k.key_prefix, k.scopes, k.expires_at,"
            " k.revoked_at, k.last_used_at, k.created_at FROM api_keys k"
        )
        params: tuple = ()
        if not global_view:
            sql += " JOIN principals p ON p.id=k.principal_id WHERE p.tenant_id=?"
            params = (_tenant_of(principal),)
        sql += " ORDER BY k.id"
        rows = conn.execute(sql, params).fetchall()
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="keys.list",
            target=str(len(rows)),
            request_id=str(getattr(request.state, "request_id", "")),
        )
        items = [
            {
                "id": int(r[0]),
                "principal_id": r[1],
                "key_prefix": str(r[2]),
                "scopes": _scope_list(r[3]),
                "expires_at": iso_utc(r[4]),
                "revoked_at": iso_utc(r[5]),
                "last_used_at": iso_utc(r[6]),
                "created_at": iso_utc(r[7]),
            }
            for r in rows
        ]
        return envelope(items)

    @app.post("/admin/keys", response_model=IssuedKey)
    def admin_keys_create(
        body: KeyCreate, request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "admin:keys")
        from libref.services.infra.auth import (
            PLATFORM_ROLE,
            create_principal,
            issue_api_key,
        )
        from libref.services.ops.audit import write_audit

        try:
            pid = create_principal(
                conn,
                caller=principal,
                tenant_id=body.tenant_id,
                kind=body.kind,
                name=body.name,
                roles=list(body.roles),
            )
            key_id, raw = issue_api_key(
                conn,
                caller=principal,
                settings=_eff_settings(request),
                principal_id=pid,
                scopes=list(body.scopes),
                expires_at=epoch_utc(body.expires_at),
            )
        except ValueError as exc:
            raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        # The platform role is exempt from separation of duties, so granting it
        # deserves its own audit action rather than blending in with tenant keys.
        action = (
            "keys.issue.platform" if PLATFORM_ROLE in list(body.roles) else "keys.issue"
        )
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action=action,
            target=str(key_id),
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        return {"id": key_id, "key": raw, "prefix": raw[:12]}

    @app.delete("/admin/keys/{key_id}", response_model=OkResponse)
    def admin_keys_revoke(
        key_id: int, request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "admin:keys")
        from libref.services.infra.auth import revoke_api_key
        from libref.services.ops.audit import write_audit

        try:
            revoke_api_key(conn, caller=principal, key_id=key_id)
        except ValueError as exc:
            write_audit(
                conn,
                tenant_id=_tenant_of(principal),
                principal_id=_principal_id(principal),
                action="keys.revoke",
                target=str(key_id),
                result="denied",
                request_id=str(getattr(request.state, "request_id", "")),
            )
            raise ApiRejection(404, "not_found", str(exc)) from exc
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="keys.revoke",
            target=str(key_id),
            request_id=str(getattr(request.state, "request_id", "")),
        )
        return {"ok": True}

    @app.delete("/admin/principals/{principal_id}/keys", response_model=RevokedKeys)
    def admin_principal_keys_revoke(
        principal_id: int, request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "admin:keys")
        from libref.services.infra.auth import revoke_principal_keys
        from libref.services.ops.audit import write_audit

        try:
            count = revoke_principal_keys(
                conn, caller=principal, principal_id=principal_id
            )
        except ValueError as exc:
            raise ApiRejection(404, "not_found", str(exc)) from exc
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="keys.revoke_all",
            target=str(principal_id),
            request_id=str(getattr(request.state, "request_id", "")),
        )
        return {"ok": True, "revoked": count}

    @app.get("/admin/quota/policies", response_model=QuotaPolicyResponse)
    def admin_quota_policies(
        request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        """List quota policies: one tenant for admins, every tenant for platform."""
        _need(principal, "admin:config")
        from libref.services.infra import quota

        scopes = set(getattr(principal, "scopes", set()) or ())
        scope_tenant = None if PLATFORM_SCOPE in scopes else _tenant_of(principal)
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            return envelope(quota.list_policies(rc, tenant_id=scope_tenant))
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.put("/admin/quota/policies", response_model=OkResponse)
    def admin_quota_policies_upsert(
        body: QuotaPolicyUpsert, request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        """Upsert one quota policy and drop the cached effective decision."""
        _need(principal, "admin:config")
        from libref.services.infra import quota
        from libref.services.infra.auth import principal_tenant
        from libref.services.ops.audit import write_audit

        target = (body.tenant_id or "").strip() or _write_tenant_of(principal)
        if not principal.can_administer(target):
            raise ApiRejection(403, "forbidden", "forbidden: target tenant")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            if body.principal_id is not None:
                owner = principal_tenant(rc, body.principal_id)
                if owner is None:
                    raise ApiRejection(
                        400,
                        "invalid_argument",
                        f"unknown principal: {body.principal_id}",
                    )
                if owner != target:
                    raise ApiRejection(
                        400,
                        "invalid_argument",
                        "principal does not belong to the target tenant",
                    )
            try:
                quota.set_policy(
                    rc,
                    tenant_id=target,
                    resource=body.resource,
                    limit_value=body.limit_value,
                    window_type=body.window_type,
                    principal_id=body.principal_id,
                )
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
            quota.invalidate_policy_cache()
            write_audit(
                rc,
                tenant_id=_tenant_of(principal),
                principal_id=_principal_id(principal),
                action="quota.policy",
                target=f"{target}:{body.principal_id or '*'}:{body.resource}",
                request_id=str(getattr(request.state, "request_id", "")),
            )
            return {"ok": True}
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.post("/admin/config/refresh", response_model=ConfigRefreshResponse)
    def admin_config_refresh(
        request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "admin:config")
        from libref.config import load_settings as _load_settings
        from libref.observe import setup_logging as _setup_logging
        from libref.services.infra.context import refresh_context as _refresh_ctx
        from libref.services.ops.audit import write_audit

        old_fp = settings_fingerprint(settings)
        try:
            fresh = _load_settings()
        except Exception as exc:
            write_audit(
                conn,
                tenant_id=_tenant_of(principal),
                principal_id=_principal_id(principal),
                action="config.refresh",
                target=old_fp[:12],
                result="denied",
                request_id=str(getattr(request.state, "request_id", "")),
            )
            raise ApiRejection(
                400, "config_refresh_failed", f"config refresh failed: {exc}"
            ) from exc
        new_fp = settings_fingerprint(fresh)
        try:
            ctx = getattr(request.app.state, "context", None)
            if ctx is not None:
                updated = _refresh_ctx(ctx, fresh)
                request.app.state.context = updated
                request.app.state.settings = fresh
            _setup_logging(fresh)
        except Exception as exc:
            write_audit(
                conn,
                tenant_id=_tenant_of(principal),
                principal_id=_principal_id(principal),
                action="config.refresh",
                target=f"{old_fp[:12]}->{new_fp[:12]}",
                result="failed",
                request_id=str(getattr(request.state, "request_id", "")),
            )
            raise ApiRejection(
                500, "config_refresh_failed", f"config rebuild failed: {exc}"
            ) from exc
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="config.refresh",
            target=f"{old_fp[:12]}->{new_fp[:12]}",
            request_id=str(getattr(request.state, "request_id", "")),
        )
        return {"ok": True, "old_fingerprint": old_fp, "fingerprint": new_fp}

    base_openapi = app.openapi

    def openapi_with_errors() -> dict[str, Any]:
        document = base_openapi()
        schemas = document.setdefault("components", {}).setdefault("schemas", {})
        error_schema = ErrorResponse.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        schemas.update(error_schema.pop("$defs", {}))
        schemas["ErrorResponse"] = error_schema
        validation_schema = ApiValidationError.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        schemas.update(validation_schema.pop("$defs", {}))
        schemas["HTTPValidationError"] = validation_schema
        schemas.pop("ValidationError", None)
        error_ref = {"$ref": "#/components/schemas/ErrorResponse"}
        for operations in document["paths"].values():
            for method, operation in operations.items():
                if method not in ("get", "post", "put", "delete", "patch"):
                    continue
                operation.setdefault("responses", {})["default"] = {
                    "description": "Rejection",
                    "content": {"application/json": {"schema": error_ref}},
                }
        return document

    app.openapi = openapi_with_errors  # type: ignore[method-assign]

    return app
