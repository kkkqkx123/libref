"""Independent graph query orchestration over documented and mined relations."""

from __future__ import annotations

from typing import Any, Literal

from libref.config import setting_or_default
from libref.services.code.mining_read import MiningReadService
from libref.services.registry.libraries import mining_version_ids
from libref.store.graph import GraphNode, GraphSubgraph, GraphView

GraphMode = Literal["neighbors", "path", "usage"]
GraphDirection = Literal["forward", "backward", "both"]

GRAPH_MODES: tuple[str, ...] = ("neighbors", "path", "usage")
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
        vids = mining_version_ids(
            self._conn, library, version, ecosystem, tenant_id=tenant_id
        )
        if not vids:
            raise ValueError(f"unknown version: {library} {version}")
        return vids

    def _view(
        self,
        vids: list[int],
        tenant_id: str | None,
        *,
        include_ambiguous: bool = False,
    ) -> GraphView:
        return GraphView(
            self._conn,
            vids,
            tenant_id=tenant_id,
            max_nodes=int(setting_or_default(self._settings, "graph_max_nodes")),
            max_fanout=int(setting_or_default(self._settings, "graph_max_fanout")),
            pattern_limit=int(
                setting_or_default(self._settings, "graph_pattern_limit")
            ),
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
        vids = self._scope(library, version, ecosystem, tenant_id)
        view = self._view(vids, tenant_id, include_ambiguous=include_ambiguous)
        seed = self._seed(view, str(entity))
        hops = self._depth(depth)
        if mode == "neighbors":
            graph = view.bfs(
                seed,
                depth=hops,
                direction=direction,
                relation_types=relation_types,
            )
            return self._result(mode, seed, graph)
        if mode == "path":
            assert target is not None
            end = self._seed(view, str(target))
            path_graph = view.shortest_path(
                seed, end, depth=hops, relation_types=relation_types
            )
            if path_graph is None:
                return {
                    "mode": mode,
                    "seed": seed.model_dump(mode="json"),
                    "target": end.model_dump(mode="json"),
                    "found": False,
                    "nodes": [],
                    "edges": [],
                    "truncated": False,
                }
            return {
                **self._result(mode, seed, path_graph),
                "target": end.model_dump(mode="json"),
                "found": True,
            }
        graph = view.bfs(
            seed,
            depth=1,
            direction="both",
            relation_types=relation_types or ["call", "mentions", "pattern_member"],
        )
        result = self._result(mode, seed, graph)
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
    def _result(mode: str, seed: GraphNode, graph: GraphSubgraph) -> dict[str, Any]:
        return {
            "mode": mode,
            "seed": seed.model_dump(mode="json"),
            "nodes": [node.model_dump(mode="json") for node in graph.nodes],
            "edges": [edge.model_dump(mode="json") for edge in graph.edges],
            "truncated": bool(graph.truncated),
        }
