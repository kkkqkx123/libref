"""Web routes: only parameter parsing plus service calls; all orchestration lives in the services layer."""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request

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
    CallInsightQuery,
    DocProbeRequest,
    FeedbackCreate,
    ImportInsightQuery,
    JobCreate,
    JobQuery,
    KeyCreate,
    LinkStatsQuery,
    PatternInsightQuery,
    ReadQuery,
    RepoProbeRequest,
    ResolveQuery,
    ReviewAction,
    ReviewQuery,
    ScrapeRequest,
    SearchQuery,
    SymbolInsightQuery,
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
    app = FastAPI(title="libref")
    app.state.context = context
    app.state.settings = settings

    def _guard(
        request=None,
        authorization: str | None = Header(default=None),
    ):
        from fastapi import Request as _Request

        req = request
        state_principal = getattr(getattr(req, "state", None), "principal", None)
        if state_principal is not None:
            return state_principal
        from libref.mcp_server.auth import parse_bearer
        from libref.services.infra.auth import dev_principal, lookup_principal

        token = getattr(settings, "mcp_token", None)
        required = bool(getattr(settings, "mcp_require_auth", False))
        auth_mode = str(getattr(settings, "auth_mode", "dev") or "dev")
        if token:
            if not verify_bearer(authorization, token):
                # In dev mode without a header, fall through to dev principal
                # only when no legacy token is configured; otherwise enforce.
                raise HTTPException(status_code=401, detail="unauthorized")
            legacy = dev_principal(settings)
            legacy.name = "legacy-token"
            return legacy
        if required and not token and auth_mode == "dev":
            raise HTTPException(status_code=500, detail="auth misconfigured")
        if auth_mode == "dev":
            return dev_principal(settings)
        raw = parse_bearer(authorization)
        if not raw:
            raise HTTPException(status_code=401, detail="unauthorized")
        principal = lookup_principal(conn, raw)
        if principal is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        return principal

    def _need(principal, *scopes: str) -> None:
        missing = [s for s in scopes if s not in set(getattr(principal, "scopes", set()))]
        if missing:
            raise HTTPException(status_code=403, detail=f"forbidden: missing {','.join(missing)}")

    def _principal_id(principal) -> int | None:
        pid = getattr(principal, "id", None)
        return int(pid) if isinstance(pid, int) else None

    def _tenant_of(principal) -> str:
        return str(getattr(principal, "tenant_id", "default") or "default")

    def _read_guard(principal=Depends(_guard)):
        _need(principal, "read")
        return None

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "fingerprint": settings_fingerprint(settings)}

    @app.get("/catalog")
    def catalog_route(
        library: str | None = None,
        ecosystem: str | None = None,
        _: None = Depends(_read_guard),
    ) -> dict[str, Any]:
        return envelope(catalog.catalog(library, ecosystem))

    @app.get("/search")
    async def search(
        query: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 8,
        preset: Literal["precise", "balanced", "thorough"] = "balanced",
        suggest: bool = False,
        include_prerelease: bool = False,
        _: None = Depends(_read_guard),
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
        try:
            results, status = await svc.search_with_mode(
                params.query,
                params.preset,
                library=params.library,
                version=params.version,
                ecosystem=params.ecosystem,
                limit=params.limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        out = envelope(results, degraded=bool(status.get("degraded", False)))
        out["reasons"] = list(status.get("reasons", []) or [])
        out["answerable"] = bool(status["answerable"])
        out["answerability"] = float(status["answerability"])
        if params.suggest and not results:
            try:
                out["suggestions"] = await discovery.search_async(
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
            raise HTTPException(status_code=502, detail=f"{exc.kind}: {exc}") from exc
        return envelope(items)

    @app.get("/answer")
    async def answer(
        query: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 5,
        preset: Literal["precise", "balanced", "thorough"] = "balanced",
        _: None = Depends(_read_guard),
    ) -> dict[str, Any]:
        from libref.services.docs.answer import answer_query_async

        params = AnswerQuery(
            query=query,
            library=library,
            version=version,
            ecosystem=ecosystem,
            limit=limit,
            preset=preset,
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
                preset=params.preset,
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
        _: None = Depends(_read_guard),
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
        _: None = Depends(_read_guard),
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
        _: None = Depends(_read_guard),
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

    @app.get("/symbol-insight")
    def symbol_insight(
        view: Literal["detail", "file", "children", "signature"],
        api: str | None = None,
        apis: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        _: None = Depends(_read_guard),
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
        try:
            return envelope(
                mining.symbol_insight(
                    params.view,
                    params.api,
                    apis=_split_list(params.apis),
                    library=params.library,
                    version=params.version,
                    ecosystem=params.ecosystem,
                    limit=params.limit,
                    offset=params.offset,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/call-insight")
    def call_insight(
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
        _: None = Depends(_read_guard),
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
        try:
            return envelope(
                mining.call_insight(
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
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/import-insight")
    def import_insight(
        view: Literal["importers", "imports"],
        api: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        _: None = Depends(_read_guard),
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
        try:
            return envelope(
                mining.import_insight(
                    params.view,
                    params.api,
                    library=params.library,
                    version=params.version,
                    ecosystem=params.ecosystem,
                    limit=params.limit,
                    offset=params.offset,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/review")
    def review_list(
        action: Literal["list"] = "list",
        limit: int = 50,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "review:read")
        params = ReviewQuery(action=action, limit=limit)
        try:
            items = review.apply(
                params.action,
                None,
                params.limit,
                tenant_id=_tenant_of(principal),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return envelope(items)

    @app.post("/review")
    def review_apply(
        body: ReviewAction, request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "review:write")
        from libref.services.ops.audit import write_audit

        try:
            result = review.apply(
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
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action=f"review.{body.action}",
            target=str(body.entity_id or ""),
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        if isinstance(result, list):
            return envelope(result)
        return result

    @app.post("/scrape")
    def scrape(
        body: ScrapeRequest, request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "ingest:write")
        from libref.services.docs.scrape_jobs import start_scrape_job
        from libref.services.ops.audit import write_audit

        mgr = JobManager(conn)
        job_id = start_scrape_job(
            mgr,
            body.library,
            body.version,
            body.start_url,
            max_pages=body.max_pages,
            ecosystem=body.ecosystem,
        )
        # Stamp ownership for cross-tenant filtering when the column exists.
        try:
            conn.execute(
                "UPDATE jobs SET tenant_id=?, principal_id=? WHERE id=?",
                (_tenant_of(principal), _principal_id(principal), job_id),
            )
            conn.commit()
        except Exception:  # noqa: BLE001 — legacy tables without new columns
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001, S110 — best effort
                pass
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="scrape.submit",
            target=f"{body.library}:{body.version}:{job_id}",
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        return {"id": job_id, "ok": True}

    @app.post("/doc-probe")
    async def doc_probe(body: DocProbeRequest, _: None = Depends(_read_guard)) -> dict[str, Any]:
        from libref.services.docs.probe import probe_docs_async

        if body.scope is not None and not isinstance(body.scope, dict):
            raise HTTPException(status_code=400, detail="scope must be an object")
        try:
            probe = await probe_docs_async(
                conn,
                settings,
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

    @app.post("/repo-probe")
    def repo_probe(body: RepoProbeRequest, _: None = Depends(_read_guard)) -> dict[str, Any]:
        from libref.services.code.probe import probe_repo

        if body.only_files is not None and (
            not body.only_files
            or any(not item.strip() for item in body.only_files)
        ):
            raise HTTPException(status_code=400, detail="only_files must be non-empty strings")
        if body.exclude_patterns is not None and (
            not body.exclude_patterns
            or any(not item.strip() for item in body.exclude_patterns)
        ):
            raise HTTPException(
                status_code=400, detail="exclude_patterns must be non-empty strings"
            )
        try:
            probe = probe_repo(
                body.source,
                settings=settings,
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

    @app.get("/jobs")
    def jobs(
        job_id: int | None = None,
        limit: int = 20,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "read")
        params = JobQuery(job_id=job_id, limit=limit)
        mgr = JobManager(conn)
        out = mgr.show(
            params.job_id, params.limit, tenant_id=_tenant_of(principal)
        )
        if "error" in out and "id" not in out:
            raise HTTPException(status_code=404, detail="job not found")
        return out

    @app.get("/jobs/{job_id}")
    def job_detail(job_id: int, principal=Depends(_guard)) -> dict[str, Any]:
        _need(principal, "read")
        mgr = JobManager(conn)
        out = mgr.show(job_id, tenant_id=_tenant_of(principal))
        if "error" in out and "id" not in out:
            raise HTTPException(status_code=404, detail="job not found")
        return out

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
            raise HTTPException(status_code=502, detail=str(exc)) from exc
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
            raise HTTPException(status_code=502, detail=f"{exc.kind}: {exc}") from exc
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
            raise HTTPException(status_code=502, detail=f"{exc.kind}: {exc}") from exc
        return envelope(items)

    @app.get("/repo-deps")
    def repo_deps(
        library: str,
        version: str,
        ecosystem: str | None = None,
        dep_ecosystem: str | None = None,
        _: None = Depends(_read_guard),
    ) -> dict[str, Any]:
        from libref.services.code.code_ingest import list_repo_dependencies

        if not library.strip():
            raise HTTPException(status_code=400, detail="library is empty")
        if not version.strip():
            raise HTTPException(status_code=400, detail="version is empty")
        try:
            rows = list_repo_dependencies(
                conn, library, version, dep_ecosystem, ecosystem=ecosystem
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return envelope(rows)

    @app.get("/link-stats")
    def link_stats(
        library: str,
        version: str,
        ecosystem: str | None = None,
        limit: int | None = None,
        url: str | None = None,
        _: None = Depends(_read_guard),
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
        try:
            if params.url:
                return page_link_stats(
                    conn,
                    params.url,
                    params.library,
                    params.version,
                    params.ecosystem,
                )
            return {
                "summary": version_link_summary(
                    conn, params.library, params.version, params.ecosystem
                ),
                "items": top_referenced(
                    conn,
                    params.library,
                    params.version,
                    params.ecosystem,
                    limit=params.limit,
                ),
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/mining-health")
    def mining_health(
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        _: None = Depends(_read_guard),
    ) -> dict[str, Any]:
        try:
            return mining.mining_health(
                library=library, version=version, ecosystem=ecosystem
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/readyz")
    def readyz() -> dict[str, Any]:
        checks: dict[str, Any] = {}
        try:
            conn.execute("SELECT 1").fetchone()
            checks["sqlite"] = "ok"
        except Exception as exc:  # noqa: BLE001 — readiness reports degraded
            checks["sqlite"] = f"unavailable: {exc}"
        try:
            from libref.services.infra.vectors import build_vector as _build_vector

            vec, _embed, _gw = _build_vector(settings)
            checks["vector"] = "disabled" if vec is None else "ok"
            checks["embed"] = "disabled" if _embed is None else "ok"
            chat_fn = _gw.chat_fn() if _gw is not None else None
            checks["chat"] = "disabled" if chat_fn is None else "ok"
        except Exception as exc:  # noqa: BLE001 — vector optional
            checks["vector"] = f"unavailable: {exc}"
        degraded = any(v != "ok" and v != "disabled" for v in checks.values())
        out: dict[str, Any] = {
            "ok": not degraded,
            "checks": checks,
            "fingerprint": settings_fingerprint(settings),
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

        svc = FeedbackService(conn)
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
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="feedback.create",
            target=str(fid),
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        return {"id": fid, "ok": True}

    @app.get("/feedback")
    def feedback_list(
        limit: int = 50,
        status: str | None = None,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "feedback:read")
        from libref.services.ops.feedback import FeedbackService

        svc = FeedbackService(conn)
        items = svc.list_items(
            tenant_id=_tenant_of(principal), limit=max(1, min(limit, 200)), status=status
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

        svc = FeedbackService(conn)
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
        from libref.services.ops.audit import write_audit

        mgr = JobManager(conn)
        job_id = mgr.submit(
            body.kind,
            body.params,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
        )
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="jobs.create",
            target=f"{body.kind}:{job_id}",
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        return {"id": job_id, "ok": True}

    @app.post("/jobs/{job_id}/cancel")
    def jobs_cancel(
        job_id: int,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "jobs:write")
        from libref.services.ops.audit import write_audit

        mgr = JobManager(conn)
        try:
            mgr.cancel(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="jobs.cancel",
            target=str(job_id),
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        return {"ok": True}

    @app.get("/admin/keys")
    def admin_keys_list(principal=Depends(_guard)) -> dict[str, Any]:
        _need(principal, "admin:keys")
        rows = conn.execute(
            "SELECT id, principal_id, key_prefix, scopes, expires_at,"
            " revoked_at, last_used_at, created_at FROM api_keys ORDER BY id"
        ).fetchall()
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

        pid = create_principal(
            conn,
            tenant_id=body.tenant_id,
            kind=body.kind,
            name=body.name,
            roles=list(body.roles),
        )
        key_id, raw = issue_api_key(
            conn,
            principal_id=pid,
            scopes=list(body.scopes),
            created_by=_principal_id(principal),
        )
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
            revoke_api_key(conn, key_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="keys.revoke",
            target=str(key_id),
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        return {"ok": True}

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
        fresh = _load_settings()
        new_fp = settings_fingerprint(fresh)
        try:
            ctx = getattr(request.app.state, "context", None)
            if ctx is not None:
                updated = _refresh_ctx(ctx, fresh)
                request.app.state.context = updated
                request.app.state.settings = fresh
            _setup_logging(fresh)
        except Exception:  # noqa: BLE001 — refresh never breaks running requests
            pass
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="config.refresh",
            target=f"{old_fp[:12]}->{new_fp[:12]}",
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        return {"ok": True, "old_fingerprint": old_fp, "fingerprint": new_fp}

    return app
