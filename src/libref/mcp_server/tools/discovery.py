"""MCP tools for discovery."""

from __future__ import annotations

from typing import Any, Literal

from libref.limits import (
    INSIGHT_ROWS,
    require_limit,
)
from libref.mcp_server.context import (
    _as_error,
    _invalid,
    _items,
)
from libref.services.code.code_ingest import list_repo_dependencies
from libref.services.code.probe import probe_repo
from libref.services.docs.probe import probe_docs_async
from libref.services.registry.resolver import resolve as _resolve


def register(mcp, ctx, wrap) -> dict[str, object]:
    conn = ctx.conn
    resolve_settings = ctx.settings
    _principal = ctx.principal
    _tenant = ctx.tenant
    _make_bundle = ctx.make_bundle
    _close = ctx.close

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

    async def resolve(
        ecosystem: str | None,
        name: str,
        version: str | None = None,
        target: Literal["docs", "repo"] = "docs",
        include_prerelease: bool = False,
    ) -> dict[str, Any]:
        """Live network call: resolve a package address against its registry in real time; results reflect the registry, not the stored knowledge base."""
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

    return {
        "doc_probe": doc_probe,
        "repo_probe": repo_probe,
        "repo_dependencies": repo_dependencies,
        "link_stats": link_stats,
        "resolve": resolve,
    }
