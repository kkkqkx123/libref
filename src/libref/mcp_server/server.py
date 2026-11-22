"""MCPServer instance and tool registration (services only)."""

from __future__ import annotations

import inspect
import logging
import sqlite3
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from libref.config import ConfigError, LibrefSettings, load_settings
from libref.crawler.registry import RegistryError
from libref.errors import LibrefError, normalize
from libref.limits import (
    ANSWER_MATERIALS,
    INSIGHT_ROWS,
    LISTING_ROWS,
    SEARCH_RESULTS,
    require_limit,
)
from libref.mcp_server.contract import ToolFailure
from libref.mcp_server.contract import failed as as_tool_failure
from libref.mcp_server.contract import install as install_contract
from libref.observe import setup_logging
from libref.services.code.code_ingest import list_repo_dependencies
from libref.services.code.mining_read import MiningReadService
from libref.services.code.probe import probe_repo
from libref.services.docs.pages import PageService
from libref.services.docs.probe import probe_docs_async
from libref.services.factory import (
    build_discovery_service,
    build_gateway,
    build_search_service,
    build_vector,
)
from libref.services.infra.auth import (
    PLATFORM_SCOPE,
    missing_scopes,
    write_tenant,
)
from libref.services.infra.tenant import GLOBAL_TENANT
from libref.services.ops.review import ReviewService
from libref.services.registry.libraries import LibraryService
from libref.services.registry.resolver import resolve as _resolve

logger = logging.getLogger(__name__)


def _invalid(
    message: str, *, field: str | None = None, hint: str | None = None
) -> ToolFailure:
    """Caller mistake: the caller can correct it, so it is reported as a failure."""
    return ToolFailure("invalid_argument", message, field=field, hint=hint)


def _not_found(message: str) -> dict[str, Any]:
    """Absent resource: a normal result, so the caller keeps its own control flow."""
    return {"error": "not_found", "message": message}


def _items(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items, "count": len(items)}


def _as_error(exc: BaseException) -> ToolFailure:
    """Turn any service exception into the single value an exit can render."""
    found = normalize(exc)
    if found is not None:
        return as_tool_failure(found)
    logger.error("tool failure: %s", exc, exc_info=exc)
    return ToolFailure("unavailable", str(exc) or type(exc).__name__)


ALL_MCP_TOOLS: frozenset[str] = frozenset(
    {
        "search",
        "library_search",
        "web_search",
        "github_search",
        "answer",
        "read_doc",
        "catalog",
        "doc_probe",
        "repo_probe",
        "repo_dependencies",
        "link_stats",
        "api_insight",
        "pattern_insight",
        "symbol_insight",
        "call_insight",
        "import_insight",
        "mining_health",
        "review",
        "job_show",
        "resolve",
        "submit_feedback",
        "request_repo_index",
        "request_docs_index",
        "index_request_status",
        "wait_index_request",
        "list_index_requests",
        "review_index_request",
    }
)


def _effective_tools(settings: LibrefSettings) -> set[str]:
    """Visible tool names after allowlist minus denylist; unknown names fail fast."""
    allowed = (
        set(settings.mcp_enabled_tools)
        if settings.mcp_enabled_tools is not None
        else set(ALL_MCP_TOOLS)
    )
    denied = set(settings.mcp_disabled_tools or [])
    unknown = (allowed | denied) - ALL_MCP_TOOLS
    if unknown:
        raise ConfigError(f"unknown MCP tools: {sorted(unknown)}")
    return allowed - denied


def build_mcp_server(
    conn: sqlite3.Connection, settings: LibrefSettings | None = None, context=None
) -> MCPServer:
    """Build the MCPServer instance and register only the visible tools."""
    resolve_settings = settings or load_settings()
    enabled = _effective_tools(resolve_settings)
    setup_logging(resolve_settings)
    mcp = MCPServer("libref")
    gateway = build_gateway(resolve_settings)
    vec_store, embed_fn, gateway = build_vector(resolve_settings, gateway)
    rerank_fn = gateway.rerank_fn()
    chat_fn = gateway.chat_fn()
    search_svc, _gateway = build_search_service(
        conn,
        resolve_settings,
        gateway=gateway,
        vec_store=vec_store,
        embed=embed_fn,
        rerank=rerank_fn,
    )
    discovery_svc = build_discovery_service(conn, resolve_settings)
    pages = PageService(conn)
    catalog_svc = LibraryService(conn)
    mining = MiningReadService(conn, resolve_settings)
    review_svc = ReviewService(conn)

    def _principal():
        from libref.services.infra.auth import current_principal, dev_principal

        found = current_principal.get()
        if found is not None:
            return found
        if (
            str(getattr(resolve_settings, "auth_mode", "enforced") or "enforced")
            == "dev"
        ):
            return dev_principal(resolve_settings)
        return None

    def _require(principal, *scopes: str) -> None:
        """Fail the call when credentials or scopes are missing."""
        if principal is None:
            raise ToolFailure(
                "unauthorized",
                "missing credentials",
                hint="send an Authorization: Bearer token",
            )
        missing = missing_scopes(principal, *scopes)
        if missing:
            from libref.services.ops.audit import write_audit

            write_audit(
                conn,
                tenant_id=write_tenant(principal),
                principal_id=getattr(principal, "id", None),
                action="auth.forbidden",
                target=",".join(missing),
                result="denied",
            )
            raise ToolFailure(
                "forbidden",
                f"missing {','.join(missing)}",
                hint="request the missing scope or use another account",
            )

    def _tenant(principal) -> str:
        scopes = set(getattr(principal, "scopes", set()) or ())
        if PLATFORM_SCOPE in scopes:
            return GLOBAL_TENANT
        return str(getattr(principal, "tenant_id", "default") or "default")

    def _make_bundle():
        ectx = context
        # Prefer app-level context when set via middleware state? MCP has no request, use build-time context.
        if ectx is not None:
            rc = ectx.connect(check_same_thread=False)
            try:
                bundle = ectx.services_for(rc)
            except Exception:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass
                raise
            return rc, bundle, True
        from libref.services.infra.jobs import JobManager
        from libref.services.ops.index_requests import IndexRequestService

        local_jobs = JobManager(conn)
        return (
            conn,
            {
                "search": search_svc,
                "pages": pages,
                "catalog": catalog_svc,
                "mining": mining,
                "review": review_svc,
                "jobs": local_jobs,
                "index_requests": IndexRequestService(
                    conn, resolve_settings, jobs=local_jobs
                ),
                "vec_store": vec_store,
                "embed": embed_fn,
                "rerank": rerank_fn,
                "chat": chat_fn,
                "gateway": gateway,
                "settings": resolve_settings,
            },
            False,
        )

    def _close(rc, should_close: bool):
        if should_close:
            try:
                rc.close()
            except Exception:  # noqa: BLE001, S110
                pass

    async def search(
        query: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 8,
        preset: Literal["precise", "balanced", "thorough"] = "balanced",
        suggest: bool = False,
        include_prerelease: bool = False,
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

    async def doc_probe(
        start_url: str,
        sample_limit: int | None = None,
        scope: dict[str, Any] | None = None,
        fetch_sample_limit: int | None = None,
        max_pages: int | None = None,
        prefilter: bool = False,
    ) -> dict[str, Any]:
        """Probe a documentation site structure without storing; confirm the range before scraping."""
        if not start_url.strip():
            raise _invalid("start_url is empty", field="start_url")
        require_limit(sample_limit, low=1, high=100)
        require_limit(fetch_sample_limit, low=0, high=100)
        require_limit(max_pages, low=1, high=100000)
        try:
            probe = await probe_docs_async(
                conn,
                resolve_settings,
                start_url,
                scope=scope,
                sample_limit=sample_limit,
                fetch_sample_limit=fetch_sample_limit,
                max_pages=max_pages,
                prefilter=prefilter,
            )
        except Exception as exc:
            raise _as_error(exc) from exc
        return probe.model_dump()

    def repo_probe(
        source: str,
        subdir: str | None = None,
        max_files: int | None = None,
        sample_limit: int | None = None,
        only_files: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        tree_depth: int | None = None,
        samples_per_dir: int | None = None,
    ) -> dict[str, Any]:
        """Probe a repository structure without parsing; confirm the range before indexing."""
        if not source.strip():
            raise _invalid("source is empty", field="source")
        require_limit(sample_limit, low=1, high=100)
        require_limit(max_files, low=1, high=100000)
        if only_files is not None and (
            not only_files or any(not item.strip() for item in only_files)
        ):
            raise _invalid(
                "only_files must be a non-empty string list", field="only_files"
            )
        if exclude_patterns is not None and (
            not exclude_patterns or any(not item.strip() for item in exclude_patterns)
        ):
            raise _invalid(
                "exclude_patterns must be a non-empty string list",
                field="exclude_patterns",
            )
        require_limit(tree_depth, low=1, high=10)
        require_limit(samples_per_dir, low=1, high=50)
        try:
            probe = probe_repo(
                source,
                settings=resolve_settings,
                subdir=subdir,
                max_files=max_files,
                sample_limit=sample_limit,
                only_files=only_files,
                exclude_patterns=exclude_patterns,
                tree_depth=tree_depth,
                samples_per_dir=samples_per_dir,
                allow_local=False,
            )
        except Exception as exc:
            raise _as_error(exc) from exc
        return probe.model_dump()

    def repo_dependencies(
        library: str,
        version: str,
        ecosystem: str | None = None,
        dep_ecosystem: str | None = None,
    ) -> dict[str, Any]:
        """Manifest-declared dependencies for one library version, rebuilt at index time."""
        if not library.strip():
            raise _invalid("library is empty", field="library")
        if not version.strip():
            raise _invalid("version is empty", field="version")
        _rcd, _bundled, _scd = _make_bundle()
        try:
            try:
                return _items(
                    list_repo_dependencies(
                        _rcd,
                        library,
                        version,
                        dep_ecosystem,
                        ecosystem=ecosystem,
                        tenant_id=_tenant(_principal()),
                    )
                )
            finally:
                _close(_rcd, _scd)
        except Exception as exc:
            raise _as_error(exc) from exc

    def link_stats(
        library: str,
        version: str,
        ecosystem: str | None = None,
        limit: int | None = None,
        url: str | None = None,
    ) -> dict[str, Any]:
        """Reference counts over stored document link edges; url selects per-page stats."""
        from libref.services.docs.links import (
            page_link_stats,
            top_referenced,
            version_link_summary,
        )

        if not library.strip():
            raise _invalid("library is empty", field="library")
        if not version.strip():
            raise _invalid("version is empty", field="version")
        require_limit(limit, low=1, high=INSIGHT_ROWS)
        _rcl, _bundlel, _scl = _make_bundle()
        try:
            try:
                if url:
                    return page_link_stats(
                        _rcl,
                        url,
                        library,
                        version,
                        ecosystem,
                        tenant_id=_tenant(_principal()),
                    )
                return {
                    "summary": version_link_summary(
                        _rcl,
                        library,
                        version,
                        ecosystem,
                        tenant_id=_tenant(_principal()),
                    ),
                    "items": top_referenced(
                        _rcl,
                        library,
                        version,
                        ecosystem,
                        limit=limit,
                        tenant_id=_tenant(_principal()),
                    ),
                }
            finally:
                _close(_rcl, _scl)
        except Exception as exc:
            raise _as_error(exc) from exc

    def api_insight(
        view: Literal["usage", "related", "args", "common"],
        api: str | None = None,
        apis: list[str] | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """API-centric usage: frequency ranking, co-occurring APIs, argument shapes."""
        if view in ("related", "args") and not api:
            raise _invalid(f"{view} view requires api")
        if view == "common" and not api and not apis:
            raise _invalid("common view requires api or apis")
        require_limit(limit, low=1, high=INSIGHT_ROWS)
        _rca, _bundlea, _sca = _make_bundle()
        try:
            try:
                return _items(
                    _bundlea["mining"].api_insight(
                        view,
                        api,
                        apis=apis,
                        library=library,
                        version=version,
                        ecosystem=ecosystem,
                        limit=limit,
                        tenant_id=_tenant(_principal()),
                    )
                )
            finally:
                _close(_rca, _sca)
        except Exception as exc:
            raise _as_error(exc) from exc

    def pattern_insight(
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
        pattern: list[str] | None = None,
        containing: list[str] | None = None,
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
    ) -> dict[str, Any]:
        """Sequence and structure insight: patterns, evidence, tree, clusters, facets."""
        if view == "evidence" and not pattern:
            raise _invalid("evidence view requires a non-empty pattern")
        require_limit(limit, low=1, high=INSIGHT_ROWS)
        require_limit(per_file_limit, low=1, high=20)
        _rcp, _bundlep, _scp = _make_bundle()
        try:
            try:
                return _items(
                    _bundlep["mining"].pattern_insight(
                        view,
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
                        tenant_id=_tenant(_principal()),
                    )
                )
            finally:
                _close(_rcp, _scp)
        except Exception as exc:
            raise _as_error(exc) from exc

    def symbol_insight(
        view: Literal["detail", "file", "children", "signature"],
        api: str | None = None,
        apis: list[str] | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Symbol detail views over version-scoped entities."""
        if view == "detail" and not api and not apis:
            raise _invalid("detail view requires api or apis")
        if view in ("file", "children", "signature") and not api:
            raise _invalid(f"{view} view requires api")
        require_limit(limit, low=1, high=INSIGHT_ROWS)
        _rcs, _bundles, _scs = _make_bundle()
        try:
            try:
                return _items(
                    _bundles["mining"].symbol_insight(
                        view,
                        api,
                        apis=apis,
                        library=library,
                        version=version,
                        ecosystem=ecosystem,
                        limit=limit,
                        offset=offset,
                        tenant_id=_tenant(_principal()),
                    )
                )
            finally:
                _close(_rcs, _scs)
        except Exception as exc:
            raise _as_error(exc) from exc

    def call_insight(
        view: Literal["callee", "caller", "file"],
        api: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        include_ambiguous: bool = False,
        resolve_kinds: list[str] | None = None,
        relation_kinds: list[str] | None = None,
        min_args: int | None = None,
        max_args: int | None = None,
    ) -> dict[str, Any]:
        """Bidirectional call views with shared mining filters."""
        if not api:
            raise _invalid(f"{view} view requires api")
        require_limit(limit, low=1, high=INSIGHT_ROWS)
        _rcc, _bundlec, _scc = _make_bundle()
        try:
            try:
                return _items(
                    _bundlec["mining"].call_insight(
                        view,
                        api,
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
                        tenant_id=_tenant(_principal()),
                    )
                )
            finally:
                _close(_rcc, _scc)
        except Exception as exc:
            raise _as_error(exc) from exc

    def import_insight(
        view: Literal["importers", "imports"],
        api: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Import edge views for fan-in and fan-out."""
        if not api:
            raise _invalid(f"{view} view requires api")
        require_limit(limit, low=1, high=INSIGHT_ROWS)
        _rci, _bundlei, _sci = _make_bundle()
        try:
            try:
                return _items(
                    _bundlei["mining"].import_insight(
                        view,
                        api,
                        library=library,
                        version=version,
                        ecosystem=ecosystem,
                        limit=limit,
                        offset=offset,
                        tenant_id=_tenant(_principal()),
                    )
                )
            finally:
                _close(_rci, _sci)
        except Exception as exc:
            raise _as_error(exc) from exc

    def mining_health(
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
    ) -> dict[str, Any]:
        """Reuse and diversity health signals computed from derived tables."""
        _rch, _bundleh, _sch = _make_bundle()
        try:
            try:
                return _bundleh["mining"].mining_health(
                    library=library,
                    version=version,
                    ecosystem=ecosystem,
                    tenant_id=_tenant(_principal()),
                )
            finally:
                _close(_rch, _sch)
        except Exception as exc:
            raise _as_error(exc) from exc

    def review(
        action: Literal["list", "approve", "conflict", "reopen"],
        entity_id: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Review workflow: list pending items or transition one item."""
        principal = _principal()
        need = "review:read" if action == "list" else "review:write"
        _require(principal, need)
        require_limit(limit, low=1, high=INSIGHT_ROWS)
        if action != "list" and entity_id is None:
            raise _invalid(f"{action} requires entity_id")
        _rcr, _bundler, _scr = _make_bundle()
        try:
            try:
                result = _bundler["review"].apply(
                    action,
                    entity_id,
                    limit,
                    tenant_id=_tenant(principal),
                    principal_id=getattr(principal, "id", None),
                )
                if action != "list":
                    from libref.services.ops.audit import write_audit

                    write_audit(
                        _rcr,
                        tenant_id=_tenant(principal),
                        principal_id=getattr(principal, "id", None),
                        action=f"review.{action}",
                        target=str(entity_id or ""),
                        request_id="",
                    )
            except ValueError as exc:
                text = str(exc)
                if "cannot transition" in text or "cannot reopen" in text:
                    raise ToolFailure("review_conflict", text, field="action") from exc
                raise _invalid(text) from exc
            except Exception as exc:
                raise _as_error(exc) from exc
        finally:
            _close(_rcr, _scr)
        if isinstance(result, list):
            return _items(result)
        return result

    def job_show(job_id: int | None = None, limit: int = 20) -> dict[str, Any]:
        """Show job results, listing recent jobs when no id is given."""
        principal = _principal()
        require_limit(limit, low=1, high=LISTING_ROWS)
        _rcj, _bundlej, _scj = _make_bundle()
        try:
            try:
                return _bundlej.get(
                    "jobs",
                    __import__(
                        "libref.services.infra.jobs", fromlist=["JobManager"]
                    ).JobManager(_rcj),
                ).show(job_id, limit, tenant_id=_tenant(principal))
            finally:
                _close(_rcj, _scj)
        except LibrefError as exc:
            if exc.code == "not_found":
                return _not_found(exc.message)
            raise
        except Exception as exc:
            raise _as_error(exc) from exc

    async def resolve(
        ecosystem: str | None,
        name: str,
        version: str | None = None,
        target: Literal["docs", "repo"] = "docs",
        include_prerelease: bool = False,
    ) -> dict[str, Any]:
        """Resolve a package address; fetches the registry and refreshes the cache."""
        from libref.crawler.http import build_async_client

        if not name.strip():
            raise _invalid("empty package name", field="name")
        try:
            async with build_async_client(resolve_settings) as client:
                resolution = await _resolve(
                    conn,
                    client,
                    resolve_settings,
                    ecosystem,
                    name,
                    version,
                    target,
                    include_prerelease,
                )
        except Exception as exc:
            raise _as_error(exc) from exc
        return resolution.model_dump()

    def submit_feedback(
        body: str,
        entity_type: str | None = None,
        entity_id: int | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """Submit developer feedback without touching review state."""
        principal = _principal()
        _require(principal, "feedback:write")
        if not (body or "").strip():
            raise _invalid("body is empty", field="body")
        try:
            from libref.services.ops.audit import write_audit
            from libref.services.ops.feedback import FeedbackService

            svc = FeedbackService(conn)
            fid = svc.submit(
                tenant_id=_tenant(principal),
                principal_id=getattr(principal, "id", None),
                entity_type=entity_type or "call_instance",
                entity_id=int(entity_id or 0),
                kind=kind or "general",
                body=body,
            )
            write_audit(
                conn,
                tenant_id=_tenant(principal),
                principal_id=getattr(principal, "id", None),
                action="feedback.create",
                target=str(fid),
                request_id="",
            )
        except Exception as exc:
            raise _as_error(exc) from exc
        return {"id": fid, "ok": True}

    def _request_service(rc, bundle):
        return bundle.get(
            "index_requests",
            __import__(
                "libref.services.ops.index_requests",
                fromlist=["IndexRequestService"],
            ).IndexRequestService(rc, resolve_settings),
        )

    def _with_next(item: dict[str, Any]) -> dict[str, Any]:
        from libref.services.ops.index_requests import IndexRequestService

        result = dict(item)
        result["next_tools"] = IndexRequestService.next_tools(item)
        if item["status"] in ("pending", "approved", "queued", "running"):
            result["pending"] = True
        return result

    async def request_repo_index(
        library: str,
        version: str | None = None,
        ecosystem: str | None = None,
        source: str | None = None,
        include_prerelease: bool = False,
        update: bool = False,
        fresh: bool = False,
        subdir: str | None = None,
        only_files: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        max_files: int | None = None,
        source_role: str | None = None,
        target_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Request repository indexing by package coordinates or an HTTP(S) Git URL."""
        principal = _principal()
        _require(principal, "index:request")
        if not library.strip():
            raise _invalid("library is empty", field="library")
        payload = {
            "kind": "repo",
            "library": library,
            "version": version or "",
            "ecosystem": ecosystem,
            "source": source,
            "include_prerelease": include_prerelease,
            "update": update,
            "fresh": fresh,
            "subdir": subdir,
            "only_files": only_files,
            "exclude_patterns": exclude_patterns,
            "max_files": max_files,
            "source_role": source_role,
            "target_filter": target_filter,
        }
        rc, bundle, should_close = _make_bundle()
        try:
            try:
                item = await _request_service(rc, bundle).submit(
                    payload, principal, tenant_id=_tenant(principal)
                )
            except Exception as exc:
                raise _as_error(exc) from exc
            return _with_next(item)
        finally:
            _close(rc, should_close)

    async def request_docs_index(
        library: str,
        version: str | None = None,
        start_url: str | None = None,
        ecosystem: str | None = None,
        include_prerelease: bool = False,
        max_pages: int | None = None,
        prefilter: bool = False,
    ) -> dict[str, Any]:
        """Request documentation indexing by coordinates or an HTTP(S) start URL."""
        principal = _principal()
        _require(principal, "index:request")
        if not library.strip():
            raise _invalid("library is empty", field="library")
        payload = {
            "kind": "docs",
            "library": library,
            "version": version or "",
            "start_url": start_url,
            "ecosystem": ecosystem,
            "include_prerelease": include_prerelease,
            "max_pages": max_pages,
            "prefilter": prefilter,
        }
        rc, bundle, should_close = _make_bundle()
        try:
            try:
                item = await _request_service(rc, bundle).submit(
                    payload, principal, tenant_id=_tenant(principal)
                )
            except Exception as exc:
                raise _as_error(exc) from exc
            return _with_next(item)
        finally:
            _close(rc, should_close)

    def index_request_status(request_id: int) -> dict[str, Any]:
        """Read one index request: status, job id, error, and the next tool to call."""
        principal = _principal()
        _require(principal, "read")
        rc, bundle, should_close = _make_bundle()
        try:
            try:
                item = _request_service(rc, bundle).get(
                    request_id, principal, tenant_id=_tenant(principal)
                )
            except Exception as exc:
                raise _as_error(exc) from exc
            return _with_next(item)
        finally:
            _close(rc, should_close)

    async def wait_index_request(
        request_id: int, timeout: float = 300.0, poll: float = 0.2
    ) -> dict[str, Any]:
        """Poll one index request up to a bounded timeout and return its state."""
        principal = _principal()
        _require(principal, "read")
        if timeout < 0 or timeout > 900:
            raise _invalid("timeout must be between 0 and 900 seconds")
        if poll <= 0 or poll > 5:
            raise _invalid("poll must be between 0 and 5 seconds")
        rc, bundle, should_close = _make_bundle()
        try:
            try:
                item = await _request_service(rc, bundle).wait(
                    request_id,
                    principal,
                    tenant_id=_tenant(principal),
                    timeout=timeout,
                    poll=poll,
                )
            except Exception as exc:
                raise _as_error(exc) from exc
            result = _with_next(item)
            from libref.services.ops.index_requests import ACTIVE_STATUSES

            if item["status"] in ACTIVE_STATUSES:
                result["timed_out"] = True
            return result
        finally:
            _close(rc, should_close)

    def list_index_requests(
        status: str | None = None, pending: bool = False, limit: int = 50
    ) -> dict[str, Any]:
        """List your own index requests, or the pending queue when allowed."""
        principal = _principal()
        _require(principal, "index:request")
        require_limit(limit, low=1, high=LISTING_ROWS)
        rc, bundle, should_close = _make_bundle()
        try:
            try:
                items = _request_service(rc, bundle).list(
                    principal,
                    tenant_id=_tenant(principal),
                    status=status,
                    pending=pending,
                    limit=limit,
                )
            except Exception as exc:
                raise _as_error(exc) from exc
            return _items(items)
        finally:
            _close(rc, should_close)

    def review_index_request(
        action: Literal["list", "approve", "reject"],
        request_id: int | None = None,
        reason: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List pending index requests, approve one, or reject one with a reason."""
        principal = _principal()
        _require(principal, "index:approve")
        require_limit(limit, low=1, high=LISTING_ROWS)
        if action != "list" and request_id is None:
            raise _invalid(f"{action} requires request_id")
        if action == "reject" and not (reason or "").strip():
            raise _invalid("reject requires a reason")
        rc, bundle, should_close = _make_bundle()
        try:
            try:
                svc = _request_service(rc, bundle)
                if action == "list":
                    items = svc.list(
                        principal,
                        tenant_id=_tenant(principal),
                        pending=True,
                        limit=limit,
                    )
                    return _items(items)
                if action == "approve":
                    assert request_id is not None
                    item = svc.approve(
                        request_id, principal, tenant_id=_tenant(principal)
                    )
                else:
                    assert request_id is not None and reason is not None
                    item = svc.reject(
                        request_id, reason, principal, tenant_id=_tenant(principal)
                    )
                return _with_next(item)
            except Exception as exc:
                raise _as_error(exc) from exc
        finally:
            _close(rc, should_close)

    _read_tools = frozenset(
        {
            "search",
            "library_search",
            "web_search",
            "github_search",
            "answer",
            "read_doc",
            "catalog",
            "doc_probe",
            "repo_probe",
            "repo_dependencies",
            "link_stats",
            "api_insight",
            "pattern_insight",
            "symbol_insight",
            "call_insight",
            "import_insight",
            "mining_health",
            "resolve",
            "job_show",
            "index_request_status",
            "wait_index_request",
        }
    )

    def _wrap(name: str, func: Any) -> Any:
        import functools as _ft

        # Only read tools need the scope guard; review/feedback do their own.
        check_scope = name in _read_tools and name not in (
            "review",
            "submit_feedback",
        )

        if inspect.iscoroutinefunction(func):

            @_ft.wraps(func)
            async def _guarded(*args: Any, **kwargs: Any) -> Any:
                if check_scope:
                    _require(_principal(), "read")
                try:
                    return await func(*args, **kwargs)
                except LibrefError as exc:
                    # A shared error (e.g. require_limit) is not a ToolError, so
                    # the runner would turn it into a crash notice without this.
                    raise as_tool_failure(exc) from exc

            _guarded.__name__ = name
            return _guarded

        @_ft.wraps(func)
        def _guarded_sync(*args: Any, **kwargs: Any) -> Any:
            if check_scope:
                _require(_principal(), "read")
            try:
                return func(*args, **kwargs)
            except LibrefError as exc:
                raise as_tool_failure(exc) from exc

        _guarded_sync.__name__ = name
        return _guarded_sync

    visible: dict[str, Any] = {
        "search": search,
        "library_search": library_search,
        "web_search": web_search,
        "github_search": github_search,
        "answer": answer,
        "read_doc": read_doc,
        "catalog": catalog,
        "doc_probe": doc_probe,
        "repo_probe": repo_probe,
        "repo_dependencies": repo_dependencies,
        "link_stats": link_stats,
        "api_insight": api_insight,
        "pattern_insight": pattern_insight,
        "symbol_insight": symbol_insight,
        "call_insight": call_insight,
        "import_insight": import_insight,
        "mining_health": mining_health,
        "review": review,
        "job_show": job_show,
        "resolve": resolve,
        "submit_feedback": submit_feedback,
        "request_repo_index": request_repo_index,
        "request_docs_index": request_docs_index,
        "index_request_status": index_request_status,
        "wait_index_request": wait_index_request,
        "list_index_requests": list_index_requests,
        "review_index_request": review_index_request,
    }
    for name, func in visible.items():
        if name in enabled:
            mcp.tool()(_wrap(name, func))

    install_contract(mcp)
    return mcp
