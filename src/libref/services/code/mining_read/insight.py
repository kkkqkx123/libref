"""Composite insight views assembled from the domain readers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from libref.limits import INSIGHT_ROWS, PER_FILE_ROWS, require_limit


class InsightReader:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    def api_insight(
        self,
        view: str,
        api: str | None = None,
        apis: list[str] | None = None,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Unified API view: frequency ranking, co-occurring APIs, argument shapes."""
        require_limit(limit, high=INSIGHT_ROWS)
        if view == "common":
            wanted = apis if apis is not None else ([api] if api else [])
            if not wanted:
                raise ValueError("common view requires api or apis")
            return self.related_common(
                wanted,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 10,
                tenant_id=tenant_id,
            )
        if view == "usage":
            return self.api_usage_counts(
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 20,
                tenant_id=tenant_id,
            )
        if view == "related":
            if not api:
                raise ValueError("related view requires api")
            return self.related_apis(
                api,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 10,
                tenant_id=tenant_id,
            )
        if view == "args":
            if not api:
                raise ValueError("args view requires api")
            return self._arg_shapes_with_facets(
                api,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 10,
                tenant_id=tenant_id,
            )
        raise ValueError(f"unsupported api view: {view}")

    def pattern_insight(
        self,
        view: str,
        pattern: list[str] | None = None,
        containing: list[str] | None = None,
        prefix: str | None = None,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int | None = None,
        per_file_limit: int | None = None,
        dedup: bool | None = None,
        profile: str | None = None,
        facet: str | None = None,
        compare_profile: str | None = None,
    ) -> list[dict[str, Any]]:
        """Unified pattern view: patterns, evidence, feature tree, clusters, facets."""
        require_limit(limit, high=INSIGHT_ROWS)
        require_limit(per_file_limit, high=PER_FILE_ROWS, name="per_file_limit")
        if view == "health":
            return [
                self.mining_health(
                    library=library,
                    version=version,
                    ecosystem=ecosystem,
                    tenant_id=tenant_id,
                    profile=profile,
                )
            ]
        if view == "labels":
            return self.list_labels(
                library=library,
                version=version,
                ecosystem=ecosystem,
                containing=containing,
                limit=limit if limit is not None else 50,
                tenant_id=tenant_id,
                profile=profile,
            )
        if view == "snippets":
            if not pattern:
                raise ValueError("snippets view requires a non-empty pattern")
            groups = self.pattern_evidence(
                pattern,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 5,
                per_file_limit=per_file_limit,
                dedup=dedup,
                with_snippet=True,
                tenant_id=tenant_id,
                profile=profile,
            )
            return [
                {
                    "caller_file": g.get("caller_file"),
                    "caller": g.get("caller"),
                    "snippet": g.get("snippet"),
                    "rendered": g.get("rendered"),
                }
                for g in groups
            ]
        if view == "patterns":
            return self.list_patterns(
                library=library,
                version=version,
                ecosystem=ecosystem,
                containing=containing,
                limit=limit if limit is not None else 50,
                tenant_id=tenant_id,
                profile=profile,
            )
        if view == "evidence":
            if not pattern:
                raise ValueError("evidence view requires a non-empty pattern")
            return self.pattern_evidence(
                pattern,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 5,
                per_file_limit=per_file_limit,
                dedup=dedup,
                with_snippet=True,
                tenant_id=tenant_id,
                profile=profile,
            )
        if view == "tree":
            return self.feature_tree(
                library=library,
                version=version,
                ecosystem=ecosystem,
                prefix=prefix,
                limit=limit if limit is not None else 100,
                tenant_id=tenant_id,
                profile=profile,
            )
        if view == "clusters":
            return self.api_clusters(
                library=library,
                version=version,
                ecosystem=ecosystem,
                containing=containing,
                limit=limit if limit is not None else 50,
                tenant_id=tenant_id,
                profile=profile,
            )
        if view == "facets":
            return self.list_facets(
                library=library,
                version=version,
                ecosystem=ecosystem,
                tenant_id=tenant_id,
                facet=facet,
                containing=containing,
                limit=limit if limit is not None else 50,
                profile=profile,
            )
        if view == "feature_summary":
            return self.facet_summary(
                library=library,
                version=version,
                ecosystem=ecosystem,
                tenant_id=tenant_id,
                limit=limit if limit is not None else 50,
                profile=profile,
            )
        if view == "cluster_labels":
            return self.list_cluster_labels(
                library=library,
                version=version,
                ecosystem=ecosystem,
                tenant_id=tenant_id,
                containing=containing,
                limit=limit if limit is not None else 50,
                profile=profile,
            )
        if view == "drift":
            return self.profile_drift(
                library=library,
                version=version,
                ecosystem=ecosystem,
                tenant_id=tenant_id,
                profile=profile,
                compare_profile=compare_profile,
                limit=limit,
            )
        raise ValueError(f"unsupported pattern view: {view}")

    def symbol_insight(
        self,
        view: str,
        api: str | None = None,
        apis: list[str] | None = None,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Thin symbol views over version-scoped entities."""
        require_limit(limit, high=INSIGHT_ROWS)
        if view == "detail":
            wanted = list(apis or []) + ([api] if api else [])
            if not wanted:
                raise ValueError("detail view requires api or apis")
            return self.symbol_details(
                wanted,
                library,
                version,
                ecosystem,
                limit=limit if limit is not None else 100,
                tenant_id=tenant_id,
            )
        if view == "file":
            if not api:
                raise ValueError("file view requires api as file path")
            return self.symbols_in_file(
                api,
                library,
                version,
                ecosystem,
                limit=limit if limit is not None else 200,
                offset=offset,
                tenant_id=tenant_id,
            )
        if view == "children":
            if not api:
                raise ValueError("children view requires api as parent name")
            return self.child_symbols(
                api,
                library,
                version,
                ecosystem,
                limit=limit if limit is not None else 200,
                offset=offset,
                tenant_id=tenant_id,
            )
        if view == "signature":
            if not api:
                raise ValueError("signature view requires api")
            return [
                self.canonical_signature(
                    api, library, version, ecosystem, tenant_id=tenant_id
                )
            ]
        raise ValueError(f"unsupported symbol view: {view}")

    def call_insight(
        self,
        view: str,
        api: str | None = None,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        include_ambiguous: bool = False,
        resolve_kinds: list[str] | None = None,
        min_args: int | None = None,
        max_args: int | None = None,
        relation_kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Thin bidirectional call views with shared filters."""
        require_limit(limit, high=INSIGHT_ROWS)
        capped = limit if limit is not None else 100
        if view == "callee":
            if not api:
                raise ValueError("callee view requires api as callee name")
            return self.calls_by_callee(
                api,
                library,
                version,
                ecosystem,
                limit=capped,
                offset=offset,
                include_ambiguous=include_ambiguous,
                resolve_kinds=resolve_kinds,
                min_args=min_args,
                max_args=max_args,
                relation_kinds=relation_kinds,
                tenant_id=tenant_id,
            )
        if view == "caller":
            if not api:
                raise ValueError("caller view requires api as caller name")
            return self.calls_by_caller(
                api,
                library,
                version,
                ecosystem,
                limit=capped,
                offset=offset,
                include_ambiguous=include_ambiguous,
                resolve_kinds=resolve_kinds,
                min_args=min_args,
                max_args=max_args,
                relation_kinds=relation_kinds,
                tenant_id=tenant_id,
            )
        if view == "file":
            if not api:
                raise ValueError("file view requires api as file path")
            return self.calls_in_file(
                api,
                library,
                version,
                ecosystem,
                limit=capped,
                offset=offset,
                include_ambiguous=include_ambiguous,
                resolve_kinds=resolve_kinds,
                min_args=min_args,
                max_args=max_args,
                relation_kinds=relation_kinds,
                tenant_id=tenant_id,
            )
        raise ValueError(f"unsupported call view: {view}")

    def import_insight(
        self,
        view: str,
        api: str | None = None,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Thin import edge views for fan-in and fan-out."""
        require_limit(limit, high=INSIGHT_ROWS)
        if view == "importers":
            if not api:
                raise ValueError("importers view requires api as target")
            return self.importers_of(
                api,
                library,
                version,
                ecosystem,
                limit=limit if limit is not None else 50,
                offset=offset,
                tenant_id=tenant_id,
            )
        if view == "imports":
            if not api:
                raise ValueError("imports view requires api as file path")
            return self.imports_of_file(
                api,
                library,
                version,
                ecosystem,
                limit=limit if limit is not None else 100,
                offset=offset,
                tenant_id=tenant_id,
            )
        raise ValueError(f"unsupported import view: {view}")
