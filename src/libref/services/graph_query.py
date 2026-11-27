"""Independent graph query orchestration over documented and mined relations."""

from __future__ import annotations

from typing import Any, Literal

from libref.config import setting_or_default
from libref.services.code.mining_read import MiningReadService
from libref.services.code.mining_read.common import _profile_context
from libref.services.registry.libraries import mining_version_ids
from libref.store.graph import GRAPH_RELATIONS, GraphNode, GraphSubgraph, GraphView
from libref.store.graph_cache import get_graph_adjacency_cache

GraphMode = Literal["neighbors", "path", "usage", "community", "impact"]
GraphDirection = Literal["forward", "backward", "both"]

GRAPH_MODES: tuple[str, ...] = ("neighbors", "path", "usage", "community", "impact")
GRAPH_DIRECTIONS: tuple[str, ...] = ("forward", "backward", "both")

_EVIDENCE_TABLES = frozenset(
    {
        "api_entities",
        "call_instances",
        "seq_patterns",
        "doc_api_links",
        "api_clusters",
        "doc_feature_node_links",
        "feature_nodes",
        "import_edges",
        "doc_link_edges",
        "doc_pattern_coverage",
    }
)


class GraphQueryService:
    """Answer structural questions without changing semantic search ranking."""

    def __init__(self, conn, settings=None, *, mining: MiningReadService | None = None):
        self._conn = conn
        self._settings = settings
        self._tenant_columns: dict[str, bool] = {}
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
        cache = None
        if setting_or_default(self._settings, "graph_cache_enabled"):
            cache = get_graph_adjacency_cache(
                int(setting_or_default(self._settings, "graph_cache_max_edges")),
                float(setting_or_default(self._settings, "graph_cache_ttl_seconds")),
            )
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
            cache=cache,
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

    @staticmethod
    def _rate(count: int, total: int) -> float:
        return round(count / total, 4) if total else 0.0

    @staticmethod
    def _average(items: list[dict[str, Any]], key: str) -> float:
        values = [float(item[key]) for item in items]
        return round(sum(values) / len(values), 4) if values else 0.0

    @staticmethod
    def _set_scores(expected: set[str], actual: set[str]) -> tuple[float, float]:
        hit = len(expected & actual)
        precision = hit / len(actual) if actual else 0.0
        recall = hit / len(expected) if expected else (1.0 if not actual else 0.0)
        return round(precision, 4), round(recall, 4)

    def _table_has_tenant(self, table: str) -> bool:
        if table not in self._tenant_columns:
            raw = getattr(self._conn, "raw", self._conn)
            probe = getattr(raw, "table_has_tenant", None)
            if probe is not None:
                self._tenant_columns[table] = bool(probe(table))
            else:
                rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                self._tenant_columns[table] = any(
                    str(row[1]) == "tenant_id" for row in rows
                )
        return self._tenant_columns[table]

    def _trace_evidence(self, ref: str) -> bool:
        table, separator, marker = str(ref).partition(":")
        if not separator or table not in _EVIDENCE_TABLES:
            return False
        row_id, separator, scope = marker.partition("@")
        if not separator or not row_id.isdigit():
            return False
        version, _, tenant = scope.partition(":")
        if not version.isdigit():
            return False
        sql = f"SELECT 1 FROM {table} WHERE id = ? AND version_id = ?"
        params: list[Any] = [int(row_id), int(version)]
        if tenant and tenant != "-" and self._table_has_tenant(table):
            sql += " AND tenant_id = ?"
            params.append(tenant)
        sql += " LIMIT 1"
        return self._conn.execute(sql, params).fetchone() is not None

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
        total_edges = sum(int(item["evidence_total"]) for item in results)
        traceable = sum(int(item["evidence_traceable"]) for item in results)
        path_cases = [item for item in results if item["expected_found"] is not None]
        path_hits = sum(1 for item in path_cases if item["found"] and item["found_ok"])
        metrics = {
            "seed_resolution": self._rate(
                sum(1 for item in results if item["seed_resolved"]), len(results)
            ),
            "path_hit": self._rate(path_hits, len(path_cases)),
            "node_precision": self._average(results, "node_precision"),
            "node_recall": self._average(results, "node_recall"),
            "relation_precision": self._average(results, "relation_precision"),
            "relation_recall": self._average(results, "relation_recall"),
            "evidence_traceability": self._rate(traceable, total_edges),
            "unexpected_truncations": sum(
                1 for item in results if not item["truncation_ok"]
            ),
        }
        return {
            "library": library,
            "version": version,
            "profile": str(profile or "usage"),
            "tenant_id": tenant_id,
            "cases": results,
            "metrics": metrics,
            "summary": {
                "total": len(results),
                "matched": matched,
                "match_rate": self._rate(matched, len(results)),
            },
        }

    def _audit_case(
        self,
        name: str,
        case: dict[str, Any],
        result: dict[str, Any] | None,
        error: str,
    ) -> dict[str, Any]:
        expected_nodes = set(case.get("expected_nodes", []))
        expected_relations = set(case.get("expected_relations", []))
        expected_refs = set(case.get("expected_evidence_refs", []))
        forbidden = set(case.get("forbidden_nodes", []))
        expected_seed = str(case.get("expected_seed") or "")
        expected_found = case.get("expected_found")
        expected_found = None if expected_found is None else bool(expected_found)
        expected_truncated = bool(case.get("expected_truncated", False))
        seed_only = bool(case.get("edges_from_seed_only", False))
        if result is None:
            return {
                "name": name,
                "mode": str(case.get("mode", "")),
                "passed": False,
                "error": error,
                "expected_seed": expected_seed,
                "actual_seed": "",
                "seed_resolved": False,
                "expected_found": expected_found,
                "found": False,
                "found_ok": False,
                "expected_nodes": sorted(expected_nodes),
                "actual_nodes": [],
                "node_precision": 0.0,
                "node_recall": 0.0,
                "forbidden_nodes": sorted(forbidden),
                "expected_relations": sorted(expected_relations),
                "actual_relations": [],
                "relation_precision": 0.0,
                "relation_recall": 0.0,
                "expected_evidence_refs": sorted(expected_refs),
                "actual_evidence_refs": [],
                "evidence_traceable": 0,
                "evidence_total": 0,
                "evidence_traceability": 0.0,
                "expected_truncated": expected_truncated,
                "truncated": False,
                "truncation_ok": False,
                "edges_from_seed_only": seed_only,
            }
        nodes = {str(node["id"]) for node in result["nodes"]}
        relations = {str(edge["relation"]) for edge in result["edges"]}
        refs = [str(edge["evidence_ref"]) for edge in result["edges"]]
        traceable = sum(self._trace_evidence(ref) for ref in refs)
        node_precision, node_recall = self._set_scores(expected_nodes, nodes)
        relation_precision, relation_recall = self._set_scores(
            expected_relations, relations
        )
        actual_seed = str(result.get("seed", {}).get("id", ""))
        seed_resolved = bool(expected_seed) and actual_seed == expected_seed
        found = bool(result.get("found", True))
        found_ok = expected_found is None or found == expected_found
        truncated = bool(result.get("truncated", False))
        truncation_ok = truncated == expected_truncated
        evidence_ok = (
            bool(refs)
            and traceable == len(refs)
            and (not expected_refs or set(refs) == expected_refs)
        )
        passed = (
            seed_resolved
            and found_ok
            and bool(expected_nodes)
            and nodes == expected_nodes
            and not forbidden & nodes
            and bool(expected_relations)
            and relations == expected_relations
            and evidence_ok
            and truncation_ok
            and (
                not seed_only
                or all(edge["source"] == actual_seed for edge in result["edges"])
            )
        )
        return {
            "name": name,
            "mode": str(result.get("mode", "")),
            "passed": passed,
            "error": error,
            "expected_seed": expected_seed,
            "actual_seed": actual_seed,
            "seed_resolved": seed_resolved,
            "expected_found": expected_found,
            "found": found,
            "found_ok": found_ok,
            "expected_nodes": sorted(expected_nodes),
            "actual_nodes": sorted(nodes),
            "node_precision": node_precision,
            "node_recall": node_recall,
            "forbidden_nodes": sorted(forbidden),
            "expected_relations": sorted(expected_relations),
            "actual_relations": sorted(relations),
            "relation_precision": relation_precision,
            "relation_recall": relation_recall,
            "expected_evidence_refs": sorted(expected_refs),
            "actual_evidence_refs": sorted(refs),
            "evidence_traceable": traceable,
            "evidence_total": len(refs),
            "evidence_traceability": self._rate(traceable, len(refs)),
            "expected_truncated": expected_truncated,
            "truncated": truncated,
            "truncation_ok": truncation_ok,
            "edges_from_seed_only": seed_only,
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
