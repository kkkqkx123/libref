"""Independent graph query orchestration over documented and mined relations."""

from __future__ import annotations

from typing import Any, Literal

from libref.config import setting_or_default
from libref.services.code.mining_read import MiningReadService
from libref.services.code.mining_read.common import _profile_context
from libref.services.registry.libraries import mining_version_ids
from libref.store.graph import GRAPH_RELATIONS, GraphNode, GraphSubgraph, GraphView

GraphMode = Literal["neighbors", "path", "usage", "community", "impact"]
GraphDirection = Literal["forward", "backward", "both"]

GRAPH_MODES: tuple[str, ...] = ("neighbors", "path", "usage", "community", "impact")
GRAPH_DIRECTIONS: tuple[str, ...] = ("forward", "backward", "both")


class GraphQueryService:
    """Answer structural questions without changing semantic search ranking."""

    def __init__(self, conn, settings=None, *, mining: MiningReadService | None = None):
        self._conn = conn
        self._settings = settings
        self._mining = (
            mining if mining is not None else MiningReadService(conn, settings)
        )

    def _scope(
        self,
        library: str | None,
        version: str | None,
        ecosystem: str | None,
        tenant_id: str | None,
    ) -> list[int]:
        if not library or not version:
            raise ValueError("library and version are required for graph queries")
        if tenant_id is None or not str(tenant_id).strip():
            raise ValueError("tenant is required for graph queries")
        vids = mining_version_ids(
            self._conn, library, version, ecosystem, tenant_id=tenant_id
        )
        if not vids:
            raise ValueError(f"unknown version: {library} {version}")
        if len(vids) != 1:
            raise ValueError(f"ambiguous version: {library} {version}")
        return vids

    def _view(
        self,
        vids: list[int],
        tenant_id: str | None,
        profile: str | None,
        *,
        include_ambiguous: bool = False,
    ) -> GraphView:
        name = str(profile or "usage")
        pids = _profile_context(self._conn, vids, name)[0]
        if not pids:
            raise ValueError(f"unknown profile: {name}")
        return GraphView(
            self._conn,
            vids,
            tenant_id=tenant_id,
            profile_ids=pids,
            profile_name=name,
            max_nodes=int(setting_or_default(self._settings, "graph_max_nodes")),
            max_fanout=int(setting_or_default(self._settings, "graph_max_fanout")),
            pattern_limit=int(
                setting_or_default(self._settings, "graph_pattern_limit")
            ),
            query_budget=int(setting_or_default(self._settings, "graph_query_budget")),
            include_ambiguous=include_ambiguous,
        )

    def _depth(self, depth: int | None) -> int:
        maximum = int(setting_or_default(self._settings, "graph_max_depth"))
        if depth is None:
            return maximum
        if depth < 1 or depth > maximum:
            raise ValueError(f"depth must be between 1 and {maximum}")
        return int(depth)

    def _seed(self, view: GraphView, name: str) -> GraphNode:
        node = view.resolve_entity(name)
        if node is None:
            raise ValueError(f"unknown entity: {name}")
        return node

    def query(
        self,
        mode: GraphMode,
        entity: str,
        *,
        target: str | None = None,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        profile: str | None = None,
        direction: GraphDirection = "both",
        depth: int | None = None,
        relation_types: list[str] | None = None,
        include_ambiguous: bool = False,
    ) -> dict[str, Any]:
        """Route one graph mode through the same bounded read view."""
        if mode not in GRAPH_MODES:
            raise ValueError(f"unsupported graph mode: {mode}")
        if direction not in GRAPH_DIRECTIONS:
            raise ValueError(f"unsupported graph direction: {direction}")
        if not str(entity).strip():
            raise ValueError("entity is empty")
        if mode == "path" and not str(target or "").strip():
            raise ValueError("target is required for path queries")
        if mode != "path" and str(target or "").strip():
            raise ValueError("target is only supported for path queries")
        if mode == "usage" and depth is not None:
            raise ValueError("depth is not supported for usage queries")
        if mode in ("community", "usage") and direction != "both":
            raise ValueError(f"{mode} queries require direction='both'")
        if mode == "impact" and direction not in ("both", "backward"):
            raise ValueError("impact queries only support backward direction")
        if relation_types is not None:
            unknown = sorted(set(relation_types) - set(GRAPH_RELATIONS))
            if unknown:
                raise ValueError(f"unsupported relation types: {', '.join(unknown)}")
        vids = self._scope(library, version, ecosystem, tenant_id)
        view = self._view(vids, tenant_id, profile, include_ambiguous=include_ambiguous)
        seed = self._seed(view, str(entity))
        hops = self._depth(depth)
        scope = {
            "library": library,
            "version": version,
            "profile": str(profile or "usage"),
            "tenant_id": tenant_id,
        }
        if mode == "neighbors":
            graph = view.bfs(
                seed,
                depth=hops,
                direction=direction,
                relation_types=relation_types,
            )
            return self._result(mode, seed, graph, scope)
        if mode == "path":
            assert target is not None
            end = self._seed(view, str(target))
            path_graph = view.shortest_path(
                seed,
                end,
                depth=hops,
                direction=direction,
                relation_types=relation_types,
            )
            if not path_graph.found:
                return {
                    "mode": mode,
                    "seed": seed.model_dump(mode="json"),
                    "target": end.model_dump(mode="json"),
                    "found": False,
                    "nodes": [],
                    "edges": [],
                    "truncated": path_graph.truncated,
                    **scope,
                }
            return {
                **self._result(mode, seed, path_graph, scope),
                "target": end.model_dump(mode="json"),
                "found": True,
            }
        if mode == "community":
            graph = view.bfs(
                seed,
                depth=hops,
                direction="both",
                relation_types=relation_types or ["member", "parent", "mentions"],
            )
            return self._result(mode, seed, graph, scope)
        if mode == "impact":
            graph = view.bfs(
                seed,
                depth=hops,
                direction="backward",
                relation_types=relation_types or ["call"],
            )
            return self._result(mode, seed, graph, scope)
        graph = view.bfs(
            seed,
            depth=1,
            direction="both",
            relation_types=relation_types
            or ["call", "mentions", "pattern_next", "covers_pattern"],
        )
        result = self._result(mode, seed, graph, scope)
        result["usage"] = {
            "related_apis": self._mining.related_apis(
                seed.label,
                library=library,
                version=version,
                ecosystem=ecosystem,
                tenant_id=tenant_id,
                limit=10,
                include_ambiguous=include_ambiguous,
            ),
            "patterns": self._mining.list_patterns(
                library=library,
                version=version,
                ecosystem=ecosystem,
                tenant_id=tenant_id,
                containing=[seed.label],
                limit=10,
                profile=profile,
            ),
            "arg_shapes": self._mining.arg_shapes(
                seed.label,
                library=library,
                version=version,
                ecosystem=ecosystem,
                tenant_id=tenant_id,
                limit=5,
                include_ambiguous=include_ambiguous,
            ),
        }
        result["documents"] = [
            node.model_dump(mode="json") for node in graph.nodes if node.kind == "doc"
        ]
        return result

    def audit(
        self,
        cases: list[dict[str, Any]],
        *,
        library: str,
        version: str,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        profile: str | None = None,
        include_ambiguous: bool = False,
    ) -> dict[str, Any]:
        """Export gold-case results for review without enforcing a quality gate."""
        results: list[dict[str, Any]] = []
        for case in cases:
            name = str(case.get("name") or "")
            result: dict[str, Any] | None = None
            error = ""
            try:
                result = self.query(
                    case["mode"],
                    case["entity"],
                    target=case.get("target"),
                    library=library,
                    version=version,
                    ecosystem=ecosystem,
                    tenant_id=tenant_id,
                    profile=profile,
                    direction=case.get("direction", "both"),
                    depth=case.get("depth"),
                    relation_types=case.get("relation_types"),
                    include_ambiguous=include_ambiguous,
                )
            except ValueError as exc:
                error = str(exc)
            results.append(self._audit_case(name, case, result, error))
        matched = sum(1 for item in results if item["passed"])
        return {
            "library": library,
            "version": version,
            "profile": str(profile or "usage"),
            "tenant_id": tenant_id,
            "cases": results,
            "summary": {"total": len(results), "matched": matched},
        }

    @staticmethod
    def _audit_case(
        name: str,
        case: dict[str, Any],
        result: dict[str, Any] | None,
        error: str,
    ) -> dict[str, Any]:
        if result is None:
            return {
                "name": name,
                "passed": False,
                "error": error,
                "expected_nodes": list(case.get("expected_nodes", [])),
                "expected_relations": list(case.get("expected_relations", [])),
                "actual_nodes": [],
                "actual_relations": [],
                "evidence_refs": [],
            }
        labels = {node["label"] for node in result["nodes"]}
        relations = {edge["relation"] for edge in result["edges"]}
        evidence_refs = [edge["evidence_ref"] for edge in result["edges"]]
        passed = (
            set(case.get("expected_nodes", [])) <= labels
            and not set(case.get("forbidden_nodes", [])) & labels
            and set(case.get("expected_relations", [])) <= relations
            and bool(evidence_refs)
        )
        if case.get("edges_from_seed_only"):
            passed = passed and all(
                edge["source"] == result["seed"]["id"] for edge in result["edges"]
            )
        return {
            "name": name,
            "passed": passed,
            "error": error,
            "expected_nodes": list(case.get("expected_nodes", [])),
            "forbidden_nodes": list(case.get("forbidden_nodes", [])),
            "expected_relations": list(case.get("expected_relations", [])),
            "actual_nodes": sorted(labels),
            "actual_relations": sorted(relations),
            "evidence_refs": evidence_refs,
            "found": result.get("found", True),
        }

    @staticmethod
    def _result(
        mode: str, seed: GraphNode, graph: GraphSubgraph, scope: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "mode": mode,
            "seed": seed.model_dump(mode="json"),
            "nodes": [node.model_dump(mode="json") for node in graph.nodes],
            "edges": [edge.model_dump(mode="json") for edge in graph.edges],
            "truncated": bool(graph.truncated),
            **scope,
        }
