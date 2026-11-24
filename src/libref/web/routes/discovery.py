"""Web routes for discovery."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import Depends, Request

from libref.crawler.registry import RegistryError
from libref.errors import LibrefError
from libref.web.api import (
    DependencyResponse,
    DiscoveryResponse,
    DocProbeRequest,
    DocProbeResponse,
    JobDetail,
    JobListResponse,
    JobQuery,
    LinkStatsQuery,
    OkIdResponse,
    PageLinkStats,
    RepoProbeRequest,
    RepoProbeResponse,
    ResolutionResponse,
    ResolveQuery,
    ScrapeRequest,
    VersionLinkStats,
    envelope,
)
from libref.web.errors import ApiRejection
from libref.web.request_context import (
    _guard,
    _principal_id,
    _read_guard,
    _tenant_of,
    _write_tenant_of,
)


def register(app, ctx) -> None:
    conn = ctx.conn
    settings = ctx.settings
    discovery = ctx.discovery
    _need = ctx.need
    _req_bundle = ctx.req_bundle
    _quota_gate = ctx.quota_gate

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
