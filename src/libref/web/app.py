"""Web routes: only parameter parsing plus service calls; all orchestration lives in the services layer."""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request

from libref.config import LibrefSettings, settings_fingerprint
from libref.crawler.registry import RegistryError
from libref.db.contract import MetadataSession
from libref.services.code.mining_read import MiningReadService
from libref.services.docs.pages import PageService
from libref.services.docs.search import SearchService
from libref.services.infra.jobs import JobManager
from libref.services.ops.review import ReviewService
from libref.services.registry.libraries import LibraryService
from libref.web.api import (
    AnswerQuery,
    AnswerResponse,
    ApiInsightQuery,
    CallInsightQuery,
    DocProbeRequest,
    Envelope,
    FeedbackCreate,
    ImportInsightQuery,
    JobCreate,
    JobQuery,
    KeyCreate,
    LinkStatsQuery,
    PageList,
    PatternInsightQuery,
    ReadQuery,
    ReadResponse,
    RepoProbeRequest,
    ResolveQuery,
    ReviewAction,
    ReviewQuery,
    ScrapeRequest,
    SearchQuery,
    SearchResponse,
    SymbolInsightQuery,
    envelope,
)
from libref.web.errors import ApiRejection


def _split_list(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    items = [part.strip() for part in raw.split(",")]
    items = [part for part in items if part]
    return items or None


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

        from libref.web.errors import error_body

        rid = str(getattr(getattr(request, "state", None), "request_id", ""))
        headers = dict(exc.headers or {})
        if rid:
            headers.setdefault("x-request-id", rid)
        return JSONResponse(
            error_body(
                exc.status_code,
                exc.detail,
                rid,
                code=str(getattr(exc, "code", "") or ""),
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
            raise HTTPException(status_code=401, detail="unauthorized")
        return principal

    def _need(principal, *scopes: str) -> None:
        missing = [
            s for s in scopes if s not in set(getattr(principal, "scopes", set()))
        ]
        if not missing:
            return
        from libref.services.ops.audit import write_audit

        write_audit(
            conn,
            tenant_id=str(getattr(principal, "tenant_id", "default") or "default"),
            principal_id=_principal_id(principal),
            action="auth.forbidden",
            target=",".join(missing),
            result="denied",
        )
        raise HTTPException(
            status_code=403, detail=f"forbidden: missing {','.join(missing)}"
        )

    def _principal_id(principal) -> int | None:
        pid = getattr(principal, "id", None)
        return int(pid) if isinstance(pid, int) else None

    def _tenant_of(principal) -> str:
        return str(getattr(principal, "tenant_id", "default") or "default")

    def _quota_gate(request: Request, principal, resource: str, cost: int = 1) -> None:
        """Reject the submission with 429 when the subject is over quota; audit on denial."""
        est = _eff_settings(request)
        if not bool(getattr(est, "quota_enabled", False)):
            return
        from libref.services.infra import quota as _quota
        from libref.services.ops.audit import write_audit as _wa

        rc, should_close, _, _bundle = _req_bundle(request, principal)
        try:
            decision = _quota.check(
                rc,
                tenant_id=_tenant_of(principal),
                principal_id=_principal_id(principal),
                resource=resource,
                cost=cost,
                settings=est,
            )
            if not decision.allowed:
                rid = str(getattr(getattr(request, "state", None), "request_id", ""))
                _quota.record_event(
                    rc,
                    tenant_id=_tenant_of(principal),
                    principal_id=_principal_id(principal),
                    resource=resource,
                    result="denied",
                    used=decision.used,
                    limit_value=decision.limit_value,
                    request_id=rid,
                )
                _wa(
                    rc,
                    tenant_id=_tenant_of(principal),
                    principal_id=_principal_id(principal),
                    action="quota",
                    target=resource,
                    result="denied",
                    request_id=rid,
                )
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "quota exceeded",
                        "resource": resource,
                        "reset_at": decision.reset_at,
                        "request_id": rid,
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
        fb = {
            "search": svc,
            "pages": pages,
            "catalog": catalog,
            "mining": mining,
            "review": review,
            "jobs": JobManager(conn),
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

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        return {"ok": True, "fingerprint": settings_fingerprint(_eff_settings(request))}

    @app.get("/me")
    def me(principal=Depends(_guard)) -> dict[str, Any]:
        return {
            "id": _principal_id(principal),
            "name": str(getattr(principal, "name", "")),
            "kind": str(getattr(principal, "kind", "")),
            "tenant_id": _tenant_of(principal),
            "roles": list(getattr(principal, "roles", []) or []),
            "scopes": sorted(set(getattr(principal, "scopes", set()) or set())),
        }

    @app.get("/metrics")
    def metrics_route(principal=Depends(_guard)) -> dict[str, Any]:
        """Read-only process counters: requests, quota denials, token usage, latency."""
        _need(principal, "read")
        from libref.services.infra.metrics import snapshot as _snapshot

        return _snapshot()

    @app.get("/catalog")
    def catalog_route(
        request: Request,
        library: str | None = None,
        ecosystem: str | None = None,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            return envelope(
                bundle["catalog"].catalog(
                    library, ecosystem, tenant_id=_tenant_of(principal)
                )
            )
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
                raise HTTPException(status_code=400, detail=str(exc)) from exc
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
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
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

    @app.get("/library-search")
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
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
                raise HTTPException(status_code=400, detail=str(exc)) from exc
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
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if page is None:
                raise HTTPException(status_code=404, detail="page not found")
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
            raise HTTPException(
                status_code=400, detail="limit must be between 1 and 200"
            )
        if offset < 0:
            raise HTTPException(status_code=400, detail="offset must be non-negative")
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

    @app.get("/api-insight")
    def api_insight(
        request: Request,
        view: Literal["usage", "related", "args", "common"],
        api: str | None = None,
        apis: str | None = None,
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
                return envelope(
                    bundle["mining"].api_insight(
                        params.view,
                        params.api,
                        apis=_split_list(params.apis),
                        library=params.library,
                        version=params.version,
                        ecosystem=params.ecosystem,
                        limit=params.limit,
                        tenant_id=_tenant_of(principal),
                    )
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/pattern-insight")
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
        pattern: str | None = None,
        containing: str | None = None,
        prefix: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        per_file_limit: int | None = None,
        dedup: bool | None = None,
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
        )
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return envelope(
                    bundle["mining"].pattern_insight(
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
                        tenant_id=_tenant_of(principal),
                    )
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/symbol-insight")
    def symbol_insight(
        request: Request,
        view: Literal["detail", "file", "children", "signature"],
        api: str | None = None,
        apis: str | None = None,
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
                return envelope(
                    bundle["mining"].symbol_insight(
                        params.view,
                        params.api,
                        apis=_split_list(params.apis),
                        library=params.library,
                        version=params.version,
                        ecosystem=params.ecosystem,
                        limit=params.limit,
                        offset=params.offset,
                        tenant_id=_tenant_of(principal),
                    )
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/call-insight")
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
        resolve_kinds: str | None = None,
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
            min_args=min_args,
            max_args=max_args,
        )
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                return envelope(
                    bundle["mining"].call_insight(
                        params.view,
                        params.api,
                        library=params.library,
                        version=params.version,
                        ecosystem=params.ecosystem,
                        limit=params.limit,
                        offset=params.offset,
                        include_ambiguous=params.include_ambiguous,
                        resolve_kinds=_split_list(params.resolve_kinds),
                        min_args=params.min_args,
                        max_args=params.max_args,
                        tenant_id=_tenant_of(principal),
                    )
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/import-insight")
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
                return envelope(
                    bundle["mining"].import_insight(
                        params.view,
                        params.api,
                        library=params.library,
                        version=params.version,
                        ecosystem=params.ecosystem,
                        limit=params.limit,
                        offset=params.offset,
                        tenant_id=_tenant_of(principal),
                    )
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/review", response_model=Envelope)
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
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return envelope(items)
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.post("/review")
    def review_apply(
        body: ReviewAction, request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "review:write")
        from libref.services.ops.audit import write_audit

        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            try:
                result = bundle["review"].apply(
                    body.action,
                    body.entity_id,
                    body.limit,
                    tenant_id=_tenant_of(principal),
                    principal_id=_principal_id(principal),
                )
            except ValueError as exc:
                text = str(exc)
                if "cannot transition" in text or "cannot reopen" in text:
                    raise HTTPException(status_code=409, detail=text) from exc
                raise HTTPException(status_code=400, detail=text) from exc
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
            if isinstance(result, list):
                return envelope(result)
            return result
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.post("/scrape")
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
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            mgr = bundle["jobs"]
            job_id = start_scrape_job(
                mgr,
                body.library,
                body.version,
                body.start_url,
                max_pages=body.max_pages,
                ecosystem=body.ecosystem,
                tenant_id=_tenant_of(principal),
                principal_id=_principal_id(principal),
            )
            write_audit(
                rc,
                tenant_id=_tenant_of(principal),
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

    @app.post("/doc-probe")
    async def doc_probe(
        request: Request, body: DocProbeRequest, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "read")
        from libref.services.docs.probe import probe_docs_async
        from libref.services.infra.tenant import validate_scrape_target
        from libref.services.ops.audit import write_audit as _wa

        if body.scope is not None and not isinstance(body.scope, dict):
            raise HTTPException(status_code=400, detail="scope must be an object")
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
                raise HTTPException(status_code=400, detail=str(exc)) from exc
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
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return probe.model_dump()
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.post("/repo-probe")
    def repo_probe(
        request: Request, body: RepoProbeRequest, principal=Depends(_guard)
    ) -> dict[str, Any]:
        from libref.services.code.probe import probe_repo

        if body.only_files is not None and (
            not body.only_files or any(not item.strip() for item in body.only_files)
        ):
            raise HTTPException(
                status_code=400, detail="only_files must be non-empty strings"
            )
        if body.exclude_patterns is not None and (
            not body.exclude_patterns
            or any(not item.strip() for item in body.exclude_patterns)
        ):
            raise HTTPException(
                status_code=400, detail="exclude_patterns must be non-empty strings"
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
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return probe.model_dump()
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/jobs")
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
            out = mgr.show(params.job_id, params.limit, tenant_id=_tenant_of(principal))
            if "error" in out and "id" not in out:
                raise HTTPException(status_code=404, detail="job not found")
            return out
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/jobs/{job_id}")
    def job_detail(
        request: Request, job_id: int, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "read")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            mgr = bundle["jobs"]
            out = mgr.show(job_id, tenant_id=_tenant_of(principal))
            if "error" in out and "id" not in out:
                raise HTTPException(status_code=404, detail="job not found")
            return out
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/resolve")
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
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RegistryError as exc:
            if exc.kind in ("no_docs", "no_repo"):
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            raise ApiRejection(502, "upstream_unavailable", str(exc)) from exc
        return resolution.model_dump()

    @app.get("/web-search")
    async def web_search(
        query: str,
        limit: int = 10,
        _: None = Depends(_read_guard),
    ) -> dict[str, Any]:
        if not query.strip():
            raise HTTPException(status_code=400, detail="query is empty")
        if limit < 1 or limit > 100:
            raise HTTPException(
                status_code=400, detail="limit must be between 1 and 100"
            )
        try:
            items = await discovery.search_web_async(query, limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RegistryError as exc:
            raise ApiRejection(
                502, "upstream_unavailable", f"{exc.kind}: {exc}"
            ) from exc
        return envelope(items)

    @app.get("/github-search")
    async def github_search(
        query: str,
        limit: int = 10,
        _: None = Depends(_read_guard),
    ) -> dict[str, Any]:
        if not query.strip():
            raise HTTPException(status_code=400, detail="query is empty")
        if limit < 1 or limit > 100:
            raise HTTPException(
                status_code=400, detail="limit must be between 1 and 100"
            )
        try:
            items = await discovery.search_github_async(query, limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RegistryError as exc:
            raise ApiRejection(
                502, "upstream_unavailable", f"{exc.kind}: {exc}"
            ) from exc
        return envelope(items)

    @app.get("/repo-deps")
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
            raise HTTPException(status_code=400, detail="library is empty")
        if not version.strip():
            raise HTTPException(status_code=400, detail="version is empty")
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
                        raise HTTPException(
                            status_code=404, detail="page not found"
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
                    raise HTTPException(status_code=404, detail=str(exc)) from exc
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return envelope(rows)
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/link-stats")
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
                    return page_link_stats(
                        rc,
                        params.url,
                        params.library,
                        params.version,
                        params.ecosystem,
                        tenant_id=_tenant_of(principal),
                    )
                return {
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
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/mining-health")
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
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.get("/readyz")
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

    @app.post("/feedback")
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
                tenant_id=_tenant_of(principal),
                principal_id=_principal_id(principal),
                entity_type=body.entity_type,
                entity_id=body.entity_id,
                kind=body.kind,
                body=body.body,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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

    @app.get("/feedback")
    def feedback_list(
        request: Request,
        limit: int = 50,
        status: str | None = None,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "feedback:read")
        from libref.services.ops.feedback import FeedbackService

        rc, should_close, est, bundle = _req_bundle(request, principal)
        svc = FeedbackService(rc)
        items = svc.list_items(
            tenant_id=_tenant_of(principal),
            limit=max(1, min(limit, 200)),
            status=status,
        )
        return envelope(items)

    @app.post("/feedback/{feedback_id}/resolve")
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
            svc.resolve(feedback_id, resolved_by=_principal_id(principal))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="feedback.resolve",
            target=str(feedback_id),
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        return {"ok": True}

    @app.post("/jobs")
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
            body.params,
            tenant_id=_tenant_of(principal),
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

    @app.post("/jobs/{job_id}/cancel")
    def jobs_cancel(
        job_id: int,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "jobs:write")
        from libref.services.ops.audit import write_audit

        rc, should_close, est, bundle = _req_bundle(request, principal)
        mgr = bundle["jobs"]
        try:
            mgr.cancel(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
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

    @app.get("/admin/keys")
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
                "scopes": str(r[3]),
                "expires_at": r[4],
                "revoked_at": r[5],
                "last_used_at": r[6],
                "created_at": r[7],
            }
            for r in rows
        ]
        return envelope(items)

    @app.post("/admin/keys")
    def admin_keys_create(
        body: KeyCreate, request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "admin:keys")
        from libref.services.infra.auth import create_principal, issue_api_key
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
                expires_at=body.expires_at,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="keys.issue",
            target=str(key_id),
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        return {"id": key_id, "key": raw, "prefix": raw[:12]}

    @app.delete("/admin/keys/{key_id}")
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
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="keys.revoke",
            target=str(key_id),
            request_id=str(getattr(request.state, "request_id", "")),
        )
        return {"ok": True}

    @app.delete("/admin/principals/{principal_id}/keys")
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
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="keys.revoke_all",
            target=str(principal_id),
            request_id=str(getattr(request.state, "request_id", "")),
        )
        return {"ok": True, "revoked": count}

    @app.post("/admin/config/refresh")
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
            raise HTTPException(
                status_code=400, detail=f"config refresh failed: {exc}"
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
            raise HTTPException(
                status_code=500, detail=f"config rebuild failed: {exc}"
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

    return app
