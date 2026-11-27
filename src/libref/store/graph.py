"""Read-only relation graph over existing source-of-truth tables.

Graph traversal is an independent query path: it materializes nodes and edges
on demand, never feeds semantic ranking, and keeps every edge bound to the
fact row it came from.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

Direction = Literal["forward", "backward", "both"]

STRONG_DOC_LINKS = frozenset({"symbol", "signature"})


class GraphNode(BaseModel):
    """A graph node with its source-of-truth identity and display fields."""

    id: str
    label: str
    kind: str
    source_table: str
    source_id: int | None = None
    version_id: int | None = None
    url: str = ""
    path: str = ""
    file: str = ""
    line_start: int | None = None
    line_end: int | None = None


class GraphEdge(BaseModel):
    """A typed relation edge with traversal confidence and source evidence."""

    id: str
    source: str
    target: str
    relation: str
    confidence: str
    weight: float = 1.0
    source_table: str
    source_id: int | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class GraphSubgraph(BaseModel):
    """Materialized nodes and induced edges returned by one graph query."""

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    truncated: bool = False


def _edge_id(source: str, target: str, relation: str, source_id: int | None) -> str:
    raw = f"{source}\x00{target}\x00{relation}\x00{source_id or 0}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class GraphView:
    """Bounded graph view over one set of library versions."""

    def __init__(
        self,
        conn,
        version_ids: list[int],
        *,
        tenant_id: str | None = None,
        max_nodes: int = 200,
        max_fanout: int = 20,
        pattern_limit: int = 200,
        include_ambiguous: bool = False,
    ) -> None:
        self._conn = conn
        self._version_ids = sorted({int(v) for v in version_ids})
        self._tenant_id = tenant_id if tenant_id not in (None, "*") else None
        self._max_nodes = max(1, int(max_nodes))
        self._max_fanout = max(1, int(max_fanout))
        self._pattern_limit = max(0, int(pattern_limit))
        self._include_ambiguous = bool(include_ambiguous)
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[str, GraphEdge] = {}
        self._tenant_columns: dict[str, bool] = {}

    @property
    def version_ids(self) -> list[int]:
        return list(self._version_ids)

    def _has_tenant(self, table: str) -> bool:
        if table not in self._tenant_columns:
            if hasattr(self._conn, "table_has_tenant"):
                self._tenant_columns[table] = bool(self._conn.table_has_tenant(table))
            else:
                try:
                    rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                except Exception:  # noqa: BLE001 -- non-SQLite sessions without tenant metadata
                    rows = []
                self._tenant_columns[table] = any(
                    str(row[1]) == "tenant_id" for row in rows
                )
        return self._tenant_columns[table]

    def _version_sql(self, column: str = "version_id") -> str:
        if not self._version_ids:
            return "1=0"
        marks = ",".join("?" for _ in self._version_ids)
        return f"{column} IN ({marks})"

    def _tenant_sql(
        self, table: str, alias: str | None = None
    ) -> tuple[str, list[str]]:
        if self._tenant_id is None or not self._has_tenant(table):
            return "", []
        prefix = f"{alias}." if alias else ""
        return f" AND {prefix}tenant_id = ?", [self._tenant_id]

    def _add_node(self, node: GraphNode) -> None:
        self._nodes.setdefault(node.id, node)

    def _add_edge(self, edge: GraphEdge) -> None:
        self._edges.setdefault(edge.id, edge)

    def _api_node(self, row: tuple) -> GraphNode:
        node_id = f"api:{int(row[0])}"
        return GraphNode(
            id=node_id,
            label=str(row[1]),
            kind=str(row[2]),
            source_table="api_entities",
            source_id=int(row[0]),
            version_id=int(row[4]) if row[4] is not None else None,
            file=str(row[3] or ""),
            line_start=int(row[5]) if row[5] else None,
            line_end=int(row[6]) if row[6] else None,
        )

    def _doc_node(self, row: tuple) -> GraphNode:
        return GraphNode(
            id=f"doc:{int(row[0])}",
            label=str(row[2] or row[1]),
            kind="doc",
            source_table="documents",
            source_id=int(row[0]),
            version_id=int(row[4]) if row[4] is not None else None,
            url=str(row[1]),
            path=str(row[3] or ""),
        )

    def _file_node(self, version_id: int, path: str) -> GraphNode:
        return GraphNode(
            id=f"file:{version_id}:{path}",
            label=path,
            kind="file",
            source_table="call_instances",
            version_id=version_id,
            file=path,
        )

    def resolve_entity(self, name: str) -> GraphNode | None:
        """Resolve an exact qualified name, then a unique simple-name suffix."""
        text = str(name).strip()
        if not text or not self._version_ids:
            return None
        tenant, params = self._tenant_sql("api_entities")
        row = self._conn.execute(
            "SELECT id, qualified_name, kind, file, version_id, line_start, line_end"
            " FROM api_entities"
            f" WHERE {self._version_sql()} AND qualified_name = ?{tenant} LIMIT 1",
            [*self._version_ids, text, *params],
        ).fetchone()
        if row is not None:
            node = self._api_node(tuple(row))
            self._add_node(node)
            return node
        rows = self._conn.execute(
            "SELECT id, qualified_name, kind, file, version_id, line_start, line_end"
            " FROM api_entities"
            f" WHERE {self._version_sql()} AND qualified_name LIKE ? ESCAPE '\\'{tenant}"
            " ORDER BY qualified_name LIMIT 100",
            [*self._version_ids, f"%{_escape_like(text)}", *params],
        ).fetchall()
        matches = [
            self._api_node(tuple(row))
            for row in rows
            if str(row[1]).endswith(f".{text}")
        ]
        if len(matches) != 1:
            return None
        self._add_node(matches[0])
        return matches[0]

    def _doc_by_id(self, doc_id: int) -> GraphNode | None:
        cached = self._nodes.get(f"doc:{doc_id}")
        if cached is not None:
            return cached
        tenant, params = self._tenant_sql("documents")
        row = self._conn.execute(
            "SELECT id, url, title, path, version_id FROM documents"
            f" WHERE id = ? AND {self._version_sql()}{tenant} LIMIT 1",
            [doc_id, *self._version_ids, *params],
        ).fetchone()
        if row is None:
            return None
        node = self._doc_node(tuple(row))
        self._add_node(node)
        return node

    def _doc_by_url(self, url: str) -> GraphNode | None:
        tenant, params = self._tenant_sql("documents")
        row = self._conn.execute(
            "SELECT id, url, title, path, version_id FROM documents"
            f" WHERE url = ? AND {self._version_sql()}{tenant} LIMIT 1",
            [url, *self._version_ids, *params],
        ).fetchone()
        if row is None:
            return None
        node = self._doc_node(tuple(row))
        self._add_node(node)
        return node

    def _entity_by_name(self, name: str) -> GraphNode | None:
        tenant, params = self._tenant_sql("api_entities")
        row = self._conn.execute(
            "SELECT id, qualified_name, kind, file, version_id, line_start, line_end"
            " FROM api_entities"
            f" WHERE {self._version_sql()} AND qualified_name = ?{tenant} LIMIT 1",
            [*self._version_ids, name, *params],
        ).fetchone()
        if row is None:
            return None
        node = self._api_node(tuple(row))
        self._add_node(node)
        return node

    def _call_edge(self, row: tuple, *, outbound: bool) -> GraphEdge | None:
        (
            call_id,
            caller_id,
            callee_id,
            callee_name,
            confidence,
            resolve_kind,
            line_s,
            line_e,
            repo,
            caller_file,
            version_id,
        ) = row
        if confidence == "AMBIGUOUS" and not self._include_ambiguous:
            return None
        if str(resolve_kind or "") in {"stdlib", "builtin"}:
            return None
        source_id = caller_id if outbound else callee_id
        target_id = callee_id if outbound else caller_id
        if source_id is not None:
            source = f"api:{int(source_id)}"
        else:
            source = f"file:{version_id}:{caller_file}"
        if target_id is not None:
            target = f"api:{int(target_id)}"
        else:
            api = self._entity_by_name(str(callee_name)) if outbound else None
            if api is None:
                return None
            target = api.id
        if not self._nodes.get(source) and source.startswith("api:"):
            self._add_node(self._api_node(self._api_row(int(source.split(":", 1)[1]))))
        if not self._nodes.get(target) and target.startswith("api:"):
            self._add_node(self._api_node(self._api_row(int(target.split(":", 1)[1]))))
        if source.startswith("file:"):
            self._add_node(self._file_node(version_id, str(caller_file)))
        level = "EXTRACTED" if confidence == "EXTRACTED" else "INFERRED"
        if confidence == "AMBIGUOUS":
            level = "AMBIGUOUS"
        return GraphEdge(
            id=_edge_id(source, target, "call", int(call_id)),
            source=source,
            target=target,
            relation="call",
            confidence=level,
            weight=1.0,
            source_table="call_instances",
            source_id=int(call_id),
            evidence={
                "callee": str(callee_name),
                "file": str(caller_file),
                "repo": str(repo or ""),
                "line_start": int(line_s or 0),
                "line_end": int(line_e or 0),
                "resolve_kind": str(resolve_kind or ""),
            },
        )

    def _api_row(self, api_id: int) -> tuple:
        row = self._conn.execute(
            "SELECT id, qualified_name, kind, file, version_id, line_start, line_end"
            " FROM api_entities WHERE id = ? LIMIT 1",
            [api_id],
        ).fetchone()
        if row is None:
            return (api_id, f"api:{api_id}", "api", "", 0, 0, 0)
        return tuple(row)

    def _api_neighbors(self, node: GraphNode, *, outbound: bool) -> list[GraphEdge]:
        if node.source_id is None:
            return []
        out: list[GraphEdge] = []
        tenant, tparams = self._tenant_sql("call_instances", "ci")
        review = (
            " LEFT JOIN review_status rs ON rs.entity_type='call_instance'"
            " AND rs.entity_id=ci.id"
        )
        column = "ci.caller_entity_id" if outbound else "ci.callee_entity_id"
        rows = self._conn.execute(
            "SELECT ci.id, ci.caller_entity_id, ci.callee_entity_id, ci.callee_name,"
            " ci.confidence, ci.resolve_kind, ci.line_start, ci.line_end, ci.repo,"
            " ci.caller_file, ci.version_id FROM call_instances ci"
            f"{review} WHERE {column} = ? AND {self._version_sql('ci.version_id')}"
            " AND ci.in_target_api = 1"
            " AND (rs.status IS NULL OR rs.status != 'conflict')"
            f"{tenant}",
            [node.source_id, *self._version_ids, *tparams],
        ).fetchall()
        for row in rows:
            edge = self._call_edge(tuple(row), outbound=outbound)
            if edge is not None:
                out.append(edge)
        if outbound:
            rows = self._conn.execute(
                "SELECT bases FROM api_entities WHERE id = ? LIMIT 1",
                [node.source_id],
            ).fetchall()
            for row in rows:
                try:
                    bases = json.loads(row[0]) if row[0] else []
                except ValueError:
                    bases = []
                for base in bases:
                    target = self._entity_by_name(str(base))
                    if target is None:
                        continue
                    out.append(
                        GraphEdge(
                            id=_edge_id(node.id, target.id, "inherit", None),
                            source=node.id,
                            target=target.id,
                            relation="inherit",
                            confidence="EXTRACTED",
                            source_table="api_entities",
                            source_id=node.source_id,
                            evidence={"base": str(base)},
                        )
                    )
            out.extend(self._pattern_edges(node, outbound=True))
        else:
            out.extend(self._doc_mentions(node))
            out.extend(self._cluster_edges(node))
            out.extend(self._feature_edges(node))
            out.extend(self._import_edges(node))
        return out

    def _pattern_edges(self, node: GraphNode, *, outbound: bool) -> list[GraphEdge]:
        if not self._pattern_limit or not node.label or not outbound:
            return []
        tenant, params = self._tenant_sql("seq_patterns")
        rows = self._conn.execute(
            "SELECT id, pattern, support FROM seq_patterns"
            f" WHERE {self._version_sql()}{tenant} ORDER BY support DESC LIMIT ?",
            [*self._version_ids, *params, self._pattern_limit],
        ).fetchall()
        out: list[GraphEdge] = []
        for pattern_id, raw, support in rows:
            try:
                names = json.loads(raw) if raw else []
            except ValueError:
                continue
            if node.label not in names:
                continue
            pattern = GraphNode(
                id=f"pattern:{int(pattern_id)}",
                label=" → ".join(str(n) for n in names),
                kind="pattern",
                source_table="seq_patterns",
                source_id=int(pattern_id),
            )
            self._add_node(pattern)
            for name in names:
                api = self._entity_by_name(str(name))
                if api is None:
                    continue
                out.append(
                    GraphEdge(
                        id=_edge_id(
                            api.id, pattern.id, "pattern_member", int(pattern_id)
                        ),
                        source=api.id,
                        target=pattern.id,
                        relation="pattern_member",
                        confidence="INFERRED",
                        weight=float(support or 1),
                        source_table="seq_patterns",
                        source_id=int(pattern_id),
                        evidence={"pattern": names, "support": int(support or 0)},
                    )
                )
        return out

    def _pattern_neighbors(self, node: GraphNode, *, outbound: bool) -> list[GraphEdge]:
        if node.source_id is None:
            return []
        tenant, params = self._tenant_sql("seq_patterns")
        row = self._conn.execute(
            "SELECT pattern, support FROM seq_patterns"
            f" WHERE id = ? AND {self._version_sql()}{tenant} LIMIT 1",
            [node.source_id, *self._version_ids, *params],
        ).fetchone()
        if row is None:
            return []
        try:
            names = json.loads(row[0]) if row[0] else []
        except ValueError:
            return []
        out: list[GraphEdge] = []
        for name in names:
            api = self._entity_by_name(str(name))
            if api is None:
                continue
            source, target = (node.id, api.id) if outbound else (api.id, node.id)
            out.append(
                GraphEdge(
                    id=_edge_id(source, target, "pattern_member", node.source_id),
                    source=source,
                    target=target,
                    relation="pattern_member",
                    confidence="INFERRED",
                    weight=float(row[1] or 1),
                    source_table="seq_patterns",
                    source_id=node.source_id,
                    evidence={"pattern": names, "support": int(row[1] or 0)},
                )
            )
        return out

    def _doc_mentions(self, node: GraphNode) -> list[GraphEdge]:
        if node.source_id is None:
            return []
        tenant, params = self._tenant_sql("doc_api_links")
        rows = self._conn.execute(
            "SELECT id, document_id, match_kind, confidence FROM doc_api_links"
            f" WHERE api_entity_id = ? AND {self._version_sql()}"
            " AND match_kind IN ('symbol','signature')"
            f"{tenant} ORDER BY confidence DESC LIMIT ?",
            [node.source_id, *self._version_ids, *params, self._max_fanout],
        ).fetchall()
        out: list[GraphEdge] = []
        for link_id, doc_id, kind, confidence in rows:
            doc = self._doc_by_id(int(doc_id))
            if doc is None:
                continue
            out.append(
                GraphEdge(
                    id=_edge_id(doc.id, node.id, "mentions", int(link_id)),
                    source=doc.id,
                    target=node.id,
                    relation="mentions",
                    confidence="EXTRACTED" if kind == "symbol" else "INFERRED",
                    weight=float(confidence or 1.0),
                    source_table="doc_api_links",
                    source_id=int(link_id),
                    evidence={"match_kind": str(kind)},
                )
            )
        return out

    def _cluster_edges(self, node: GraphNode) -> list[GraphEdge]:
        tenant, params = self._tenant_sql("api_clusters")
        rows = self._conn.execute(
            "SELECT id, cluster_id FROM api_clusters"
            f" WHERE member = ? AND {self._version_sql()}{tenant} LIMIT ?",
            [node.label, *self._version_ids, *params, self._max_fanout],
        ).fetchall()
        out: list[GraphEdge] = []
        for cluster_row_id, cluster_id in rows:
            cluster = GraphNode(
                id=f"cluster:{int(cluster_row_id)}",
                label=f"cluster:{int(cluster_id)}",
                kind="cluster",
                source_table="api_clusters",
                source_id=int(cluster_row_id),
            )
            self._add_node(cluster)
            out.append(
                GraphEdge(
                    id=_edge_id(cluster.id, node.id, "member", int(cluster_row_id)),
                    source=cluster.id,
                    target=node.id,
                    relation="member",
                    confidence="INFERRED",
                    source_table="api_clusters",
                    source_id=int(cluster_row_id),
                    evidence={"cluster_id": int(cluster_id)},
                )
            )
        return out

    def _cluster_neighbors(self, node: GraphNode) -> list[GraphEdge]:
        if node.source_id is None:
            return []
        tenant, params = self._tenant_sql("api_clusters")
        row = self._conn.execute(
            "SELECT member FROM api_clusters"
            f" WHERE id = ? AND {self._version_sql()}{tenant} LIMIT 1",
            [node.source_id, *self._version_ids, *params],
        ).fetchone()
        if row is None:
            return []
        api = self._entity_by_name(str(row[0]))
        if api is None:
            return []
        return [
            GraphEdge(
                id=_edge_id(node.id, api.id, "member", node.source_id),
                source=node.id,
                target=api.id,
                relation="member",
                confidence="INFERRED",
                source_table="api_clusters",
                source_id=node.source_id,
                evidence={"member": str(row[0])},
            )
        ]

    def _feature_edges(self, node: GraphNode) -> list[GraphEdge]:
        if node.source_id is None:
            return []
        tenant_l, params_l = self._tenant_sql("doc_feature_node_links", "l")
        tenant_a, params_a = self._tenant_sql("doc_api_links", "a")
        rows = self._conn.execute(
            "SELECT DISTINCT l.id, l.node_path FROM doc_feature_node_links l"
            " JOIN doc_api_links a ON a.document_id=l.document_id"
            f" AND a.version_id=l.version_id WHERE a.api_entity_id = ?"
            f" AND {self._version_sql('l.version_id')}"
            " AND a.match_kind IN ('symbol','signature')"
            f"{tenant_l}{tenant_a} LIMIT ?",
            [
                node.source_id,
                *self._version_ids,
                *params_l,
                *params_a,
                self._max_fanout,
            ],
        ).fetchall()
        out: list[GraphEdge] = []
        for link_id, path in rows:
            feature = GraphNode(
                id=f"feature:{path}",
                label=str(path),
                kind="feature",
                source_table="doc_feature_node_links",
                source_id=int(link_id),
            )
            self._add_node(feature)
            out.append(
                GraphEdge(
                    id=_edge_id(feature.id, node.id, "feature_context", int(link_id)),
                    source=feature.id,
                    target=node.id,
                    relation="feature_context",
                    confidence="INFERRED",
                    source_table="doc_feature_node_links",
                    source_id=int(link_id),
                    evidence={"node_path": str(path)},
                )
            )
        return out

    def _import_edges(self, node: GraphNode) -> list[GraphEdge]:
        tenant, params = self._tenant_sql("import_edges")
        rows = self._conn.execute(
            "SELECT id, importer_file, version_id FROM import_edges"
            f" WHERE target = ? AND {self._version_sql()}{tenant} LIMIT ?",
            [node.label, *self._version_ids, *params, self._max_fanout],
        ).fetchall()
        out: list[GraphEdge] = []
        for edge_id, path, vid in rows:
            file_node = self._file_node(int(vid), str(path))
            self._add_node(file_node)
            out.append(
                GraphEdge(
                    id=_edge_id(file_node.id, node.id, "import", int(edge_id)),
                    source=file_node.id,
                    target=node.id,
                    relation="import",
                    confidence="EXTRACTED",
                    source_table="import_edges",
                    source_id=int(edge_id),
                    evidence={"file": str(path), "target": node.label},
                )
            )
        return out

    def _file_neighbors(self, node: GraphNode) -> list[GraphEdge]:
        if not node.file:
            return []
        tenant, params = self._tenant_sql("import_edges")
        rows = self._conn.execute(
            "SELECT id, target FROM import_edges"
            f" WHERE importer_file = ? AND {self._version_sql()}{tenant} LIMIT ?",
            [node.file, *self._version_ids, *params, self._max_fanout],
        ).fetchall()
        out: list[GraphEdge] = []
        for edge_id, target in rows:
            api = self._entity_by_name(str(target))
            if api is None:
                continue
            out.append(
                GraphEdge(
                    id=_edge_id(node.id, api.id, "import", int(edge_id)),
                    source=node.id,
                    target=api.id,
                    relation="import",
                    confidence="EXTRACTED",
                    source_table="import_edges",
                    source_id=int(edge_id),
                    evidence={"file": node.file, "target": str(target)},
                )
            )
        return out

    def _doc_neighbors(self, node: GraphNode, *, outbound: bool) -> list[GraphEdge]:
        if not outbound or not node.url:
            return []
        out: list[GraphEdge] = []
        tenant, params = self._tenant_sql("doc_api_links")
        rows = self._conn.execute(
            "SELECT id, api_entity_id, match_kind, confidence FROM doc_api_links"
            f" WHERE document_id = ? AND {self._version_sql()}"
            " AND match_kind IN ('symbol','signature')"
            f"{tenant} ORDER BY confidence DESC LIMIT ?",
            [node.source_id, *self._version_ids, *params, self._max_fanout],
        ).fetchall()
        for link_id, api_id, kind, confidence in rows:
            api_row = self._api_row(int(api_id))
            api = self._api_node(api_row)
            self._add_node(api)
            out.append(
                GraphEdge(
                    id=_edge_id(node.id, api.id, "mentions", int(link_id)),
                    source=node.id,
                    target=api.id,
                    relation="mentions",
                    confidence="EXTRACTED" if kind == "symbol" else "INFERRED",
                    weight=float(confidence or 1.0),
                    source_table="doc_api_links",
                    source_id=int(link_id),
                    evidence={"match_kind": str(kind)},
                )
            )
        tenant, params = self._tenant_sql("doc_link_edges")
        links = self._conn.execute(
            "SELECT dst_url FROM doc_link_edges"
            f" WHERE src_url = ? AND {self._version_sql()}{tenant} LIMIT ?",
            [node.url, *self._version_ids, *params, self._max_fanout],
        ).fetchall()
        for (dst,) in links:
            target = self._doc_by_url(str(dst))
            if target is None:
                continue
            out.append(
                GraphEdge(
                    id=_edge_id(node.id, target.id, "doc_link", None),
                    source=node.id,
                    target=target.id,
                    relation="doc_link",
                    confidence="INFERRED",
                    source_table="doc_link_edges",
                    evidence={"dst_url": str(dst)},
                )
            )
        return out

    def _feature_neighbors(self, node: GraphNode) -> list[GraphEdge]:
        return []

    def neighbors(self, node: GraphNode, *, direction: Direction) -> list[GraphEdge]:
        """Return edges adjacent to one node in the requested direction."""
        if node.source_table == "api_entities":
            if direction in ("forward", "both"):
                yield_from = self._api_neighbors(node, outbound=True)
            else:
                yield_from = []
            if direction in ("backward", "both"):
                yield_from = [*yield_from, *self._api_neighbors(node, outbound=False)]
            return yield_from
        if node.source_table == "documents":
            return self._doc_neighbors(node, outbound=direction != "backward")
        if node.source_table == "call_instances" and node.kind == "file":
            return self._file_neighbors(node) if direction != "backward" else []
        if node.source_table == "seq_patterns":
            return self._pattern_neighbors(node, outbound=direction != "backward")
        if node.source_table == "api_clusters":
            return self._cluster_neighbors(node) if direction != "backward" else []
        if node.source_table == "doc_feature_node_links":
            return self._feature_neighbors(node)
        return []

    @staticmethod
    def _priority(edge: GraphEdge) -> int:
        return 10 if edge.relation in {"call", "inherit", "mentions", "import"} else 1

    @staticmethod
    def _target_of(edge: GraphEdge, node_id: str) -> str:
        return edge.target if edge.source == node_id else edge.source

    def _accept(self, edge: GraphEdge, relation_types: frozenset[str] | None) -> bool:
        if relation_types is not None and edge.relation not in relation_types:
            return False
        return not (edge.confidence == "AMBIGUOUS" and not self._include_ambiguous)

    def bfs(
        self,
        root: GraphNode,
        *,
        depth: int,
        direction: Direction = "both",
        relation_types: list[str] | None = None,
    ) -> GraphSubgraph:
        """Bounded breadth-first expansion from one seed node."""
        wanted = frozenset(relation_types) if relation_types else None
        self._add_node(root)
        visited = {root.id}
        frontier: list[tuple[str, int]] = [(root.id, 0)]
        truncated = False
        while frontier:
            node_id, hop = frontier.pop(0)
            if hop >= max(0, int(depth)):
                continue
            node = self._nodes.get(node_id)
            if node is None:
                continue
            candidates: dict[tuple[str, str], GraphEdge] = {}
            for edge in self.neighbors(node, direction=direction):
                if not self._accept(edge, wanted):
                    continue
                other = self._target_of(edge, node_id)
                if other == node_id:
                    continue
                key = (other, edge.relation)
                current = candidates.get(key)
                rank = (self._priority(edge), edge.weight, edge.id)
                if current is None or rank > (
                    self._priority(current),
                    current.weight,
                    current.id,
                ):
                    candidates[key] = edge
            for edge in sorted(
                candidates.values(),
                key=lambda e: (self._priority(e), e.weight, e.id),
                reverse=True,
            )[: self._max_fanout]:
                other = self._target_of(edge, node_id)
                if other not in self._nodes and len(self._nodes) >= self._max_nodes:
                    truncated = True
                    continue
                self._add_edge(edge)
                if other not in self._nodes:
                    neighbor = GraphNode(
                        id=other,
                        label=edge.evidence.get("callee") or other,
                        kind="api" if other.startswith("api:") else "derived",
                        source_table=edge.source_table,
                    )
                    self._add_node(neighbor)
                if other not in visited:
                    visited.add(other)
                    frontier.append((other, hop + 1))
        return self._result(truncated)

    def shortest_path(
        self,
        start: GraphNode,
        end: GraphNode,
        *,
        depth: int,
        relation_types: list[str] | None = None,
    ) -> GraphSubgraph | None:
        """Bounded shortest path using the graph's forward edge direction."""
        wanted = frozenset(relation_types) if relation_types else None
        self._add_node(start)
        self._add_node(end)
        if start.id == end.id:
            return self._result(False)
        frontier: list[tuple[str, int]] = [(start.id, 0)]
        seen = {start.id}
        parents: dict[str, tuple[str, GraphEdge]] = {}
        truncated = False
        while frontier and len(seen) < self._max_nodes:
            node_id, hop = frontier.pop(0)
            if hop >= max(0, int(depth)):
                continue
            node = self._nodes.get(node_id)
            if node is None:
                continue
            candidates: dict[str, GraphEdge] = {}
            for edge in self.neighbors(node, direction="both"):
                if not self._accept(edge, wanted):
                    continue
                other = self._target_of(edge, node_id)
                current = candidates.get(other)
                rank = (self._priority(edge), edge.weight, edge.id)
                if current is None or rank > (
                    self._priority(current),
                    current.weight,
                    current.id,
                ):
                    candidates[other] = edge
            for target, edge in candidates.items():
                self._add_edge(edge)
                if target not in seen:
                    seen.add(target)
                    parents[target] = (node_id, edge)
                    if target == end.id:
                        return self._path_result(start.id, end.id, parents, truncated)
                    frontier.append((target, hop + 1))
            if len(candidates) > self._max_fanout:
                truncated = True
        return None

    def _path_result(
        self,
        start: str,
        end: str,
        parents: dict[str, tuple[str, GraphEdge]],
        truncated: bool,
    ) -> GraphSubgraph:
        keep_edges: list[GraphEdge] = []
        keep_nodes = {start, end}
        node_id = end
        while node_id in parents:
            prev, edge = parents[node_id]
            keep_edges.append(edge)
            keep_nodes.add(prev)
            node_id = prev
        self._edges = {edge.id: edge for edge in keep_edges if edge.id in self._edges}
        self._nodes = {
            node_id: node
            for node_id, node in self._nodes.items()
            if node_id in keep_nodes
        }
        return self._result(truncated)

    def _result(self, truncated: bool) -> GraphSubgraph:
        return GraphSubgraph(
            nodes=sorted(self._nodes.values(), key=lambda n: n.id),
            edges=sorted(self._edges.values(), key=lambda e: (-e.weight, e.id)),
            truncated=truncated or len(self._nodes) >= self._max_nodes,
        )
