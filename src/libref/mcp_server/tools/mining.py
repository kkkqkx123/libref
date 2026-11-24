"""MCP tools for mining."""

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


def register(mcp, ctx, wrap) -> dict[str, object]:
    _principal = ctx.principal
    _tenant = ctx.tenant
    _make_bundle = ctx.make_bundle
    _close = ctx.close

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

    def doc_insight(
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
    ) -> dict[str, Any]:
        """Document insight: block facets, API alignment, pattern coverage, and verdicts."""
        require_limit(limit, low=1, high=INSIGHT_ROWS)
        _rcd, _bundled, _scd = _make_bundle()
        try:
            try:
                return _items(
                    _bundled["doc_insight"].insight(
                        library,
                        version,
                        view=view,
                        ecosystem=ecosystem,
                        profile=profile,
                        facet=facet,
                        api=api,
                        pattern=pattern,
                        verdict=verdict,
                        block_type=block_type,
                        document_id=document_id,
                        limit=limit,
                        tenant_id=_tenant(_principal()),
                    )
                )
            finally:
                _close(_rcd, _scd)
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

    return {
        "api_insight": api_insight,
        "pattern_insight": pattern_insight,
        "doc_insight": doc_insight,
        "symbol_insight": symbol_insight,
        "call_insight": call_insight,
        "import_insight": import_insight,
        "mining_health": mining_health,
    }
