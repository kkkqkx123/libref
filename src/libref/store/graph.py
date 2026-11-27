"""Read-only relation graph over existing source-of-truth tables.

Graph traversal is an independent query path: it materializes nodes and edges
on demand, never feeds semantic ranking, and keeps every edge bound to the
fact row it came from.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, Field

from libref.store.graph_cache import CacheKey, GraphAdjacencyCache, graph_cache_context

logger = logging.getLogger(__name__)

Direction = Literal["forward", "backward", "both"]

STRONG_DOC_LINKS = ("symbol", "signature")
GRAPH_RELATIONS = (
    "call",
    "import",
    "inherit",
    "mentions",
    "covers_pattern",
    "member",
    "parent",
    "doc_link",
    "pattern_next",
)

_SNAPSHOT_TABLES = (
    "api_entities",
    "call_instances",
    "review_status",
    "seq_patterns",
    "doc_api_links",
    "api_clusters",
    "feature_nodes",
    "doc_feature_node_links",
    "import_edges",
    "documents",
    "doc_link_edges",
    "doc_pattern_coverage",
    "doc_consistency",
)


class GraphNode(BaseModel):
    """A graph node with its source-of-truth identity and display fields."""

    id: str
    label: str
    kind: str
    source_table: str
    source_id: int | None = None
    version_id: int | None = None
    profile_id: int | None = None
    tenant_id: str | None = None
    confidence: str = ""
    evidence_ref: str = ""
    weight: float = 1.0
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
    version_id: int | None = None
    profile_id: int | None = None
    tenant_id: str | None = None
    evidence_ref: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)


class GraphSubgraph(BaseModel):
    """Materialized nodes and induced edges returned by one graph query."""

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    truncated: bool = False
    found: bool = True


_NODE_FIELDS = tuple(GraphNode.model_fields)
_EDGE_FIELDS = tuple(GraphEdge.model_fields)
_EDGE_SOURCE_INDEX = _EDGE_FIELDS.index("source")
_EDGE_TARGET_INDEX = _EDGE_FIELDS.index("target")

_NodeRecord = tuple[Any, ...]
_NodeVariants = tuple[tuple[str, tuple[_NodeRecord, ...]], ...]


@dataclass(frozen=True, slots=True)
class _EdgeRecord:
    """Immutable edge values plus node descriptions added while deriving it."""

    values: tuple[Any, ...]
    nodes: _NodeVariants
    rank: tuple[int, float, str]
    direction: Direction
    anchor: str

    @property
    def source(self) -> str:
        return str(self.values[_EDGE_SOURCE_INDEX])

    @property
    def target(self) -> str:
        return str(self.values[_EDGE_TARGET_INDEX])


def _pack_node(node: GraphNode) -> _NodeRecord:
    return tuple(getattr(node, name) for name in _NODE_FIELDS)


def _unpack_node(record: _NodeRecord) -> GraphNode:
    return GraphNode(**dict(zip(_NODE_FIELDS, record, strict=True)))


def _pack_edge(
    edge: GraphEdge,
    nodes: _NodeVariants = (),
    *,
    direction: Direction = "forward",
    anchor: str = "",
) -> _EdgeRecord:
    values: list[Any] = []
    for name in _EDGE_FIELDS:
        value = getattr(edge, name)
        if isinstance(value, dict):
            value = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        values.append(value)
    rank = (
        10 if edge.relation in {"call", "inherit", "mentions", "import"} else 1,
        edge.weight,
        edge.id,
    )
    return _EdgeRecord(tuple(values), nodes, rank, direction, anchor)


def _unpack_edge(record: _EdgeRecord) -> GraphEdge:
    values = dict(zip(_EDGE_FIELDS, record.values, strict=True))
    values["evidence"] = json.loads(values["evidence"])
    return GraphEdge(**values)


def _merge_edge_records(old: _EdgeRecord, new: _EdgeRecord) -> _EdgeRecord:
    variants = dict(old.nodes)
    for node_id, records in new.nodes:
        variants[node_id] = tuple(dict.fromkeys((*variants.get(node_id, ()), *records)))
    return _EdgeRecord(
        old.values, tuple(sorted(variants.items())), old.rank, old.direction, old.anchor
    )


class _AdjacencySnapshot:
    """Immutable forward and backward adjacency indexes for one fact scope."""

    def __init__(
        self,
        records: dict[tuple[str, Direction], _EdgeRecord],
        *,
        table_scans: int = 0,
    ) -> None:
        self._records = tuple(records.values())
        self.table_scans = int(table_scans)
        forward: dict[str, list[_EdgeRecord]] = {}
        backward: dict[str, list[_EdgeRecord]] = {}
        for record in sorted(self._records, key=lambda item: item.rank, reverse=True):
            index = forward if record.direction == "forward" else backward
            index.setdefault(record.anchor, []).append(record)
        self.forward: Mapping[str, tuple[_EdgeRecord, ...]] = MappingProxyType(
            {key: tuple(value) for key, value in forward.items()}
        )
        self.backward: Mapping[str, tuple[_EdgeRecord, ...]] = MappingProxyType(
            {key: tuple(value) for key, value in backward.items()}
        )

    @property
    def edge_count(self) -> int:
        return len(self._records)


@dataclass(frozen=True, slots=True)
class _PatternCandidate:
    """One adjacent pattern pair, kept for deterministic representative choice."""

    pattern_id: int
    support: int
    position: int
    names: tuple[str, ...]
    version_id: int
    profile_id: int

    @property
    def rank(self) -> tuple[int, int, int]:
        return (int(self.support), -int(self.pattern_id), -int(self.position))


def _digest(raw: str) -> str:
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _edge_id(
    source: str,
    target: str,
    relation: str,
    source_id: int | None,
    evidence_key: str = "",
) -> str:
    raw = f"{source}\x00{target}\x00{relation}\x00{source_id or 0}\x00{evidence_key}"
    return _digest(raw)


def _evidence_ref(
    source_table: str,
    source_id: int | None,
    version_id: int | None,
    tenant_id: str | None,
    evidence_key: str,
) -> str:
    raw = f"{source_table}|{source_id or 0}|{version_id or 0}|{tenant_id or ''}|{evidence_key}"
    marker = str(source_id) if source_id is not None else _digest(raw)
    return f"{source_table}:{marker}@{version_id or 0}:{tenant_id or '-'}"


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pattern_digest(names: list[str]) -> str:
    return _digest(json.dumps(names, ensure_ascii=False))


def _strong_link_sql(alias: str | None = None) -> tuple[str, list[str]]:
    prefix = f"{alias}." if alias else ""
    marks = ",".join("?" for _ in STRONG_DOC_LINKS)
    return f"{prefix}match_kind IN ({marks})", list(STRONG_DOC_LINKS)


class _PatternIndex:
    """Query-local index of merged adjacent pairs from sampled patterns."""

    def __init__(self) -> None:
        self._by_source: dict[tuple[bool, str], dict[str, _PatternCandidate]] = {}
        self._nodes: dict[tuple[int, str], GraphNode] = {}

    def add(
        self,
        rows: list[tuple],
        resolve: Callable[[set[str]], dict[tuple[int, str], GraphNode]],
    ) -> None:
        names: set[str] = set()
        for _, raw, _, _, _ in rows:
            try:
                names.update(str(name) for name in json.loads(raw) if str(name))
            except TypeError, ValueError:
                continue
        self._nodes = resolve(names)
        for pattern_id, raw, support, version_id, profile_id in rows:
            try:
                parsed = (
                    [str(name) for name in json.loads(raw) if str(name)] if raw else []
                )
            except TypeError, ValueError:
                continue
            for position in range(len(parsed) - 1):
                previous, current = parsed[position], parsed[position + 1]
                candidate = _PatternCandidate(
                    pattern_id=int(pattern_id),
                    support=int(support or 0),
                    position=position,
                    names=tuple(parsed),
                    version_id=int(version_id),
                    profile_id=int(profile_id),
                )
                for outbound, source, target in (
                    (True, previous, current),
                    (False, current, previous),
                ):
                    bucket = self._by_source.setdefault((outbound, source), {})
                    current_best = bucket.get(target)
                    if current_best is None or candidate.rank > current_best.rank:
                        bucket[target] = candidate

    def candidates(
        self, label: str, *, outbound: bool
    ) -> list[tuple[str, _PatternCandidate]]:
        return list(self._by_source.get((outbound, label), {}).items())

    def node(self, version_id: int, name: str) -> GraphNode | None:
        return self._nodes.get((version_id, name))


class _EmptyCursor:
    """Cursor shape for reads skipped after the query budget is exhausted."""

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _QueryBudget:
    """Count fact-table reads issued during one bounded traversal."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self.used = 0
        self.exhausted = False
        self.active = False

    def take(self) -> bool:
        if not self.active:
            return True
        if self.used >= self.limit:
            self.exhausted = True
            return False
        self.used += 1
        return True


class _BudgetedConnection:
    """Connection surface that enforces the active traversal query budget."""

    def __init__(self, conn, budget: _QueryBudget) -> None:
        self._conn = conn
        self._budget = budget

    def execute(self, sql: str, params: Any = ()) -> Any:
        if sql.lstrip().upper().startswith("PRAGMA"):
            return self._conn.execute(sql, params)
        if not self._budget.take():
            return _EmptyCursor()
        return self._conn.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class GraphView:
    """Bounded graph view over one version and one mining profile."""

    def __init__(
        self,
        conn,
        version_ids: list[int],
        *,
        tenant_id: str | None = None,
        profile_ids: list[int] | None = None,
        profile_name: str = "usage",
        max_nodes: int = 200,
        max_fanout: int = 20,
        pattern_limit: int = 200,
        query_budget: int = 400,
        include_ambiguous: bool = False,
        cache: GraphAdjacencyCache | None = None,
    ) -> None:
        self._raw_conn = conn
        self._budget = _QueryBudget(query_budget)
        self._conn = _BudgetedConnection(conn, self._budget)
        self._version_ids = sorted({int(v) for v in version_ids})
        if len(self._version_ids) != 1:
            raise ValueError("graph view requires one exact version")
        self._tenant_id = tenant_id if tenant_id not in (None, "*") else None
        self._profile_ids = sorted({int(p) for p in (profile_ids or [])})
        self._profile_name = str(profile_name or "usage")
        self._max_nodes = max(1, int(max_nodes))
        self._max_fanout = max(1, int(max_fanout))
        self._pattern_limit = max(0, int(pattern_limit))
        self._include_ambiguous = bool(include_ambiguous)
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[str, GraphEdge] = {}
        self._tenant_columns: dict[str, bool] = {}
        self._truncated = False
        self._cache = cache
        self._cache_lease: Any = None
        self._snapshot: _AdjacencySnapshot | None = None
        self._pattern_index: _PatternIndex | None = None

    def _open_budget(self) -> None:
        self._truncated = False
        self._budget.used = 0
        self._budget.exhausted = False
        self._budget.active = True
        self._prepare_snapshot()

    def _close_budget(self) -> None:
        self._budget.active = False
        if self._budget.exhausted:
            self._truncated = True
        self._release_snapshot()

    def _prepare_snapshot(self) -> None:
        if self._cache is None or self._snapshot is not None:
            return
        context = graph_cache_context(self._raw_conn)
        if context is None:
            logger.debug("graph cache fallback: watermark unavailable")
            return
        backend, identity, watermark = context
        key = CacheKey(
            backend=backend,
            identity=identity,
            tenant=self._tenant_id,
            version=self.version_id,
            profiles=tuple(self._profile_ids),
            profile_name=self._profile_name,
            include_ambiguous=self._include_ambiguous,
            pattern_limit=self._pattern_limit,
            watermark=watermark,
        )
        cache = self._cache
        lease = cache.acquire(key, lambda: self._build_snapshot(cache.max_edges))
        if lease is None:
            return
        self._cache_lease = lease
        self._snapshot = lease.snapshot

    def _release_snapshot(self) -> None:
        if self._cache_lease is not None:
            self._cache_lease.release()
            self._cache_lease = None
        self._snapshot = None

    @property
    def version_id(self) -> int:
        return self._version_ids[0]

    @property
    def profile_id(self) -> int | None:
        return self._profile_ids[0] if len(self._profile_ids) == 1 else None

    def _has_tenant(self, table: str) -> bool:
        if table not in self._tenant_columns:
            if hasattr(self._conn, "table_has_tenant"):
                self._tenant_columns[table] = bool(self._conn.table_has_tenant(table))
            else:
                try:
                    rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                except Exception:  # noqa: BLE001 -- sessions without tenant metadata
                    rows = []
                self._tenant_columns[table] = any(
                    str(row[1]) == "tenant_id" for row in rows
                )
        return self._tenant_columns[table]

    def _version_sql(self, column: str = "version_id") -> str:
        return f"{column} IN ({','.join('?' for _ in self._version_ids)})"

    def _profile_sql(self, column: str = "profile_id") -> str:
        if not self._profile_ids:
            return "1=0"
        marks = ",".join("?" for _ in self._profile_ids)
        return f"{column} IN ({marks})"

    def _tenant_sql(
        self, table: str, alias: str | None = None
    ) -> tuple[str, list[str]]:
        if self._tenant_id is None or not self._has_tenant(table):
            return "", []
        prefix = f"{alias}." if alias else ""
        return f" AND {prefix}tenant_id = ?", [self._tenant_id]

    def _review_sql(self) -> tuple[str, list[str]]:
        review = (
            " LEFT JOIN review_status rs ON rs.entity_type='call_instance'"
            " AND rs.entity_id=ci.id"
        )
        if self._tenant_id is not None and self._has_tenant("review_status"):
            review += " AND rs.tenant_id = ?"
            return review, [self._tenant_id]
        return review, []

    def _add_node(self, node: GraphNode) -> None:
        self._nodes.setdefault(node.id, node)

    def _add_edge(self, edge: GraphEdge) -> None:
        self._edges.setdefault(edge.id, edge)

    def _make_edge(
        self,
        *,
        source: str,
        target: str,
        relation: str,
        confidence: str,
        source_table: str,
        source_id: int | None = None,
        version_id: int | None = None,
        profile_id: int | None = None,
        weight: float = 1.0,
        evidence: dict[str, Any] | None = None,
        evidence_key: str = "",
    ) -> GraphEdge:
        return GraphEdge(
            id=_edge_id(source, target, relation, source_id, evidence_key),
            source=source,
            target=target,
            relation=relation,
            confidence=confidence,
            weight=float(weight),
            source_table=source_table,
            source_id=source_id,
            version_id=version_id,
            profile_id=profile_id,
            tenant_id=self._tenant_id,
            evidence_ref=_evidence_ref(
                source_table, source_id, version_id, self._tenant_id, evidence_key
            ),
            evidence=evidence or {},
        )

    def _api_node(self, row: tuple) -> GraphNode:
        node_id = f"api:{int(row[0])}"
        version_id = int(row[4]) if row[4] is not None else None
        return GraphNode(
            id=node_id,
            label=str(row[1]),
            kind="api",
            source_table="api_entities",
            source_id=int(row[0]),
            version_id=version_id,
            profile_id=self.profile_id,
            tenant_id=self._tenant_id,
            confidence="EXTRACTED",
            evidence_ref=_evidence_ref(
                "api_entities", int(row[0]), version_id, self._tenant_id, ""
            ),
            file=str(row[3] or ""),
            line_start=int(row[5]) if row[5] else None,
            line_end=int(row[6]) if row[6] else None,
        )

    def _doc_node(self, row: tuple) -> GraphNode:
        doc_id = int(row[0])
        version_id = int(row[4]) if row[4] is not None else None
        return GraphNode(
            id=f"doc:{doc_id}",
            label=str(row[2] or row[1]),
            kind="doc",
            source_table="documents",
            source_id=doc_id,
            version_id=version_id,
            profile_id=self.profile_id,
            tenant_id=self._tenant_id,
            confidence="EXTRACTED",
            evidence_ref=_evidence_ref(
                "documents", doc_id, version_id, self._tenant_id, ""
            ),
            url=str(row[1]),
            path=str(row[3] or ""),
        )

    def _file_node(self, version_id: int, path: str) -> GraphNode:
        return GraphNode(
            id=f"file:{version_id}:{path}",
            label=path,
            kind="file",
            source_table="file",
            version_id=version_id,
            profile_id=self.profile_id,
            tenant_id=self._tenant_id,
            confidence="EXTRACTED",
            evidence_ref=_evidence_ref("file", None, version_id, self._tenant_id, path),
            file=path,
        )

    def _pattern_node(
        self,
        *,
        names: list[str],
        digest: str,
        source_table: str,
        source_id: int,
        version_id: int,
        profile_id: int,
        support: int,
    ) -> GraphNode:
        return GraphNode(
            id=f"pattern:{profile_id}:{digest}",
            label=" → ".join(str(n) for n in names),
            kind="pattern",
            source_table=source_table,
            source_id=source_id,
            version_id=version_id,
            profile_id=profile_id,
            tenant_id=self._tenant_id,
            confidence="INFERRED",
            evidence_ref=_evidence_ref(
                source_table, source_id, version_id, self._tenant_id, digest
            ),
            weight=float(support),
            path=digest,
        )

    def _cluster_node(
        self,
        *,
        cluster_id: int,
        source_id: int,
        version_id: int,
        profile_id: int,
        size: int,
    ) -> GraphNode:
        return GraphNode(
            id=f"cluster:{profile_id}:{cluster_id}",
            label=f"cluster:{cluster_id}",
            kind="cluster",
            source_table="api_clusters",
            source_id=source_id,
            version_id=version_id,
            profile_id=profile_id,
            tenant_id=self._tenant_id,
            confidence="INFERRED",
            evidence_ref=_evidence_ref(
                "api_clusters", source_id, version_id, self._tenant_id, ""
            ),
            weight=float(size),
            path=str(cluster_id),
        )

    def _feature_node(
        self, path: str, version_id: int, profile_id: int, source_id: int | None
    ) -> GraphNode:
        return GraphNode(
            id=f"feature:{profile_id}:{path}",
            label=path,
            kind="feature",
            source_table="feature_nodes",
            source_id=source_id,
            version_id=version_id,
            profile_id=profile_id,
            tenant_id=self._tenant_id,
            confidence="INFERRED",
            evidence_ref=_evidence_ref(
                "feature_nodes", source_id, version_id, self._tenant_id, path
            ),
            path=path,
        )

    def resolve_entity(self, name: str) -> GraphNode | None:
        """Resolve an exact qualified name, then a unique simple-name suffix."""
        text = str(name).strip()
        if not text:
            return None
        tenant, params = self._tenant_sql("api_entities")
        exact = self._conn.execute(
            "SELECT id, qualified_name, kind, file, version_id, line_start, line_end"
            " FROM api_entities"
            f" WHERE {self._version_sql()} AND qualified_name = ?{tenant}"
            " ORDER BY id",
            [*self._version_ids, text, *params],
        ).fetchall()
        if exact:
            if len(exact) != 1:
                return None
            node = self._api_node(tuple(exact[0]))
            self._add_node(node)
            return node
        rows = self._conn.execute(
            "SELECT id, qualified_name, kind, file, version_id, line_start, line_end"
            " FROM api_entities"
            f" WHERE {self._version_sql()} AND qualified_name LIKE ? ESCAPE '\\'{tenant}"
            " ORDER BY qualified_name, id",
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

    def _docs_by_url(self, url: str) -> list[GraphNode]:
        tenant, params = self._tenant_sql("documents")
        rows = self._conn.execute(
            "SELECT id, url, title, path, version_id FROM documents"
            f" WHERE url = ? AND {self._version_sql()}{tenant}"
            " ORDER BY sort_order, id LIMIT ?",
            [url, *self._version_ids, *params, self._max_fanout + 1],
        ).fetchall()
        if len(rows) > self._max_fanout:
            self._truncated = True
        nodes = [self._doc_node(tuple(row)) for row in rows[: self._max_fanout]]
        for node in nodes:
            self._add_node(node)
        return nodes

    def _entity_by_name(self, name: str, version_id: int) -> GraphNode | None:
        tenant, params = self._tenant_sql("api_entities")
        rows = self._conn.execute(
            "SELECT id, qualified_name, kind, file, version_id, line_start, line_end"
            " FROM api_entities"
            f" WHERE qualified_name = ? AND version_id = ?{tenant}"
            " ORDER BY id",
            [name, version_id, *params],
        ).fetchall()
        if len(rows) != 1:
            return None
        node = self._api_node(tuple(rows[0]))
        self._add_node(node)
        return node

    def _api_row(self, api_id: int, version_id: int) -> tuple | None:
        tenant, params = self._tenant_sql("api_entities")
        row = self._conn.execute(
            "SELECT id, qualified_name, kind, file, version_id, line_start, line_end"
            " FROM api_entities WHERE id = ? AND version_id = ?"
            f"{tenant} LIMIT 1",
            [api_id, version_id, *params],
        ).fetchone()
        if row is None:
            return None
        return tuple(row)

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
        if (
            str(resolve_kind or "") in {"stdlib", "builtin"}
            and not self._include_ambiguous
        ):
            return None
        source_id = caller_id if outbound else callee_id
        target_id = callee_id if outbound else caller_id
        if source_id is not None:
            source = f"api:{int(source_id)}"
        else:
            source = f"file:{version_id}:{caller_file}"
            self._add_node(self._file_node(version_id, str(caller_file)))
        if target_id is not None:
            target = f"api:{int(target_id)}"
        elif outbound:
            target_node = self._entity_by_name(str(callee_name), version_id)
            if target_node is None:
                return None
            target = target_node.id
        else:
            target = f"file:{version_id}:{caller_file}"
            self._add_node(self._file_node(version_id, str(caller_file)))
        for node_id in (source, target):
            if node_id.startswith("api:") and node_id not in self._nodes:
                api_id = int(node_id.split(":", 1)[1])
                row_data = self._api_row(api_id, version_id)
                if row_data is None:
                    return None
                self._add_node(self._api_node(row_data))
        level = "EXTRACTED" if confidence == "EXTRACTED" else "INFERRED"
        if confidence == "AMBIGUOUS":
            level = "AMBIGUOUS"
        return self._make_edge(
            source=source,
            target=target,
            relation="call",
            confidence=level,
            source_table="call_instances",
            source_id=int(call_id),
            version_id=version_id,
            profile_id=self.profile_id,
            evidence={
                "callee": str(callee_name),
                "file": str(caller_file),
                "repo": str(repo or ""),
                "line_start": int(line_s or 0),
                "line_end": int(line_e or 0),
                "resolve_kind": str(resolve_kind or ""),
            },
        )

    def _api_neighbors(self, node: GraphNode, *, outbound: bool) -> list[GraphEdge]:
        if node.source_id is None or node.version_id is None:
            return []
        out: list[GraphEdge] = []
        review, review_params = self._review_sql()
        tenant, tparams = self._tenant_sql("call_instances", "ci")
        column = "ci.caller_entity_id" if outbound else "ci.callee_entity_id"
        rows = self._conn.execute(
            "SELECT ci.id, ci.caller_entity_id, ci.callee_entity_id, ci.callee_name,"
            " ci.confidence, ci.resolve_kind, ci.line_start, ci.line_end, ci.repo,"
            " ci.caller_file, ci.version_id FROM call_instances ci"
            f"{review} WHERE {column} = ? AND {self._version_sql('ci.version_id')}"
            " AND ci.in_target_api = 1"
            " AND (rs.status IS NULL OR rs.status != 'conflict')"
            f"{tenant}"
            " ORDER BY ci.id",
            [*review_params, node.source_id, *self._version_ids, *tparams],
        ).fetchall()
        calls: list[GraphEdge] = []
        for row in rows:
            edge = self._call_edge(tuple(row), outbound=outbound)
            if edge is not None:
                calls.append(edge)
        grouped: dict[tuple[str, str], list[GraphEdge]] = {}
        for edge in calls:
            grouped.setdefault((edge.source, edge.target), []).append(edge)
        for group in grouped.values():
            representative = group[0]
            representative.weight = float(len(group))
            representative.evidence["call_count"] = len(group)
            out.append(representative)
        tenant, params = self._tenant_sql("api_entities")
        bases = self._conn.execute(
            "SELECT bases FROM api_entities WHERE id = ?"
            f" AND {self._version_sql()}{tenant} LIMIT 1",
            [node.source_id, *self._version_ids, *params],
        ).fetchone()
        if bases is not None:
            try:
                names = json.loads(bases[0]) if bases[0] else []
            except ValueError:
                names = []
            for name in names:
                target = self._entity_by_name(str(name), node.version_id)
                if target is None:
                    continue
                out.append(
                    self._make_edge(
                        source=node.id,
                        target=target.id,
                        relation="inherit",
                        confidence="EXTRACTED",
                        source_table="api_entities",
                        source_id=node.source_id,
                        version_id=node.version_id,
                        profile_id=node.profile_id,
                        evidence={"base": str(name)},
                    )
                )
        out.extend(self._pattern_edges(node, outbound=outbound))
        if not outbound:
            out.extend(self._doc_mentions(node))
            out.extend(self._cluster_edges(node))
            out.extend(self._feature_edges(node))
            out.extend(self._import_edges(node))
        return out

    def _resolve_pattern_names(
        self, names: set[str]
    ) -> dict[tuple[int, str], GraphNode]:
        if not names:
            return {}
        tenant, params = self._tenant_sql("api_entities")
        marks = ",".join("?" for _ in names)
        rows = self._conn.execute(
            "SELECT id, qualified_name, kind, file, version_id, line_start, line_end"
            " FROM api_entities"
            f" WHERE {self._version_sql()} AND qualified_name IN ({marks}){tenant}"
            " ORDER BY qualified_name, id",
            [*self._version_ids, *sorted(names), *params],
        ).fetchall()
        grouped: dict[tuple[int, str], list[tuple]] = {}
        for row in rows:
            grouped.setdefault((int(row[4]), str(row[1])), []).append(tuple(row))
        resolved: dict[tuple[int, str], GraphNode] = {}
        for key, group in grouped.items():
            if len(group) != 1:
                continue
            node = self._api_node(group[0])
            self._add_node(node)
            resolved[key] = node
        return resolved

    def _ensure_pattern_index(self) -> _PatternIndex:
        if self._pattern_index is not None:
            return self._pattern_index
        index = _PatternIndex()
        if self._pattern_limit:
            tenant, params = self._tenant_sql("seq_patterns")
            profile = self._profile_sql()
            rows = [
                tuple(row)
                for row in self._conn.execute(
                    "SELECT id, pattern, support, version_id, profile_id FROM seq_patterns"
                    f" WHERE {self._version_sql()} AND {profile}{tenant}"
                    " ORDER BY support DESC, id LIMIT ?",
                    [
                        *self._version_ids,
                        *self._profile_ids,
                        *params,
                        self._pattern_limit,
                    ],
                ).fetchall()
            ]
            index.add(rows, self._resolve_pattern_names)
        self._pattern_index = index
        return index

    def _pattern_edges(self, node: GraphNode, *, outbound: bool) -> list[GraphEdge]:
        if not self._pattern_limit or not node.label or node.version_id is None:
            return []
        index = self._ensure_pattern_index()
        merged: dict[str, tuple[_PatternCandidate, GraphNode]] = {}
        for target_name, candidate in index.candidates(node.label, outbound=outbound):
            target = index.node(candidate.version_id, target_name)
            if target is None:
                continue
            previous = merged.get(target.id)
            if previous is None or candidate.rank > previous[0].rank:
                merged[target.id] = (candidate, target)
        out: list[GraphEdge] = []
        for candidate, target in merged.values():
            out.append(
                self._make_edge(
                    source=node.id,
                    target=target.id,
                    relation="pattern_next",
                    confidence="INFERRED",
                    source_table="seq_patterns",
                    source_id=candidate.pattern_id,
                    version_id=candidate.version_id,
                    profile_id=candidate.profile_id,
                    weight=float(candidate.support or 1),
                    evidence={
                        "pattern": list(candidate.names),
                        "support": candidate.support,
                        "position": candidate.position,
                        "pattern_id": candidate.pattern_id,
                    },
                )
            )
        return out

    def _doc_mentions(self, node: GraphNode) -> list[GraphEdge]:
        if node.source_id is None or node.version_id is None:
            return []
        tenant, params = self._tenant_sql("doc_api_links")
        strong, strong_params = _strong_link_sql()
        rows = self._conn.execute(
            "SELECT id, document_id, match_kind, confidence, version_id"
            " FROM doc_api_links"
            f" WHERE api_entity_id = ? AND {self._version_sql()}"
            f" AND {strong}{tenant} ORDER BY confidence DESC LIMIT ?",
            [
                node.source_id,
                *self._version_ids,
                *strong_params,
                *params,
                self._max_fanout,
            ],
        ).fetchall()
        out: list[GraphEdge] = []
        for link_id, doc_id, kind, confidence, version_id in rows:
            doc = self._doc_by_id(int(doc_id))
            if doc is None:
                continue
            out.append(
                self._make_edge(
                    source=doc.id,
                    target=node.id,
                    relation="mentions",
                    confidence="EXTRACTED" if kind == "symbol" else "INFERRED",
                    source_table="doc_api_links",
                    source_id=int(link_id),
                    version_id=int(version_id),
                    profile_id=node.profile_id,
                    weight=float(confidence or 1.0),
                    evidence={"match_kind": str(kind)},
                )
            )
        return out

    def _cluster_edges(self, node: GraphNode) -> list[GraphEdge]:
        if node.version_id is None:
            return []
        tenant, params = self._tenant_sql("api_clusters")
        profile = self._profile_sql()
        rows = self._conn.execute(
            "SELECT id, cluster_id, size, representative, version_id, profile_id"
            " FROM api_clusters"
            f" WHERE member = ? AND {self._version_sql()} AND {profile}{tenant}"
            " ORDER BY cluster_id LIMIT ?",
            [
                node.label,
                *self._version_ids,
                *self._profile_ids,
                *params,
                self._max_fanout,
            ],
        ).fetchall()
        out: list[GraphEdge] = []
        for row_id, cluster_id, size, representative, version_id, profile_id in rows:
            cluster = self._cluster_node(
                cluster_id=int(cluster_id),
                source_id=int(row_id),
                version_id=int(version_id),
                profile_id=int(profile_id),
                size=int(size),
            )
            self._add_node(cluster)
            out.append(
                self._make_edge(
                    source=cluster.id,
                    target=node.id,
                    relation="member",
                    confidence="INFERRED",
                    source_table="api_clusters",
                    source_id=int(row_id),
                    version_id=int(version_id),
                    profile_id=int(profile_id),
                    evidence={
                        "cluster_id": int(cluster_id),
                        "representative": str(representative or ""),
                        "size": int(size),
                    },
                )
            )
        return out

    def _cluster_neighbors(self, node: GraphNode) -> list[GraphEdge]:
        if not node.path or node.version_id is None:
            return []
        cluster_id = int(node.path)
        tenant, params = self._tenant_sql("api_clusters")
        profile = self._profile_sql()
        rows = self._conn.execute(
            "SELECT id, member, size, representative, version_id, profile_id"
            " FROM api_clusters"
            f" WHERE cluster_id = ? AND {self._version_sql()} AND {profile}{tenant}"
            " ORDER BY member LIMIT ?",
            [
                cluster_id,
                *self._version_ids,
                *self._profile_ids,
                *params,
                self._max_fanout,
            ],
        ).fetchall()
        out: list[GraphEdge] = []
        for row_id, member, size, representative, version_id, profile_id in rows:
            api = self._entity_by_name(str(member), int(version_id))
            if api is None:
                continue
            out.append(
                self._make_edge(
                    source=node.id,
                    target=api.id,
                    relation="member",
                    confidence="INFERRED",
                    source_table="api_clusters",
                    source_id=int(row_id),
                    version_id=int(version_id),
                    profile_id=int(profile_id),
                    evidence={
                        "cluster_id": cluster_id,
                        "representative": str(representative or ""),
                        "size": int(size),
                    },
                )
            )
        return out

    def _feature_edges(self, node: GraphNode) -> list[GraphEdge]:
        if node.source_id is None or node.version_id is None:
            return []
        tenant_l, params_l = self._tenant_sql("doc_feature_node_links", "l")
        tenant_a, params_a = self._tenant_sql("doc_api_links", "a")
        profile = self._profile_sql("l.profile_id")
        strong, strong_params = _strong_link_sql("a")
        rows = self._conn.execute(
            "SELECT DISTINCT l.id, l.node_path, l.version_id, l.profile_id"
            " FROM doc_feature_node_links l"
            " JOIN doc_api_links a ON a.document_id=l.document_id"
            " AND a.version_id=l.version_id"
            f" WHERE a.api_entity_id = ? AND {self._version_sql('l.version_id')}"
            f" AND {profile} AND {strong}{tenant_l}{tenant_a} LIMIT ?",
            [
                node.source_id,
                *self._version_ids,
                *self._profile_ids,
                *strong_params,
                *params_l,
                *params_a,
                self._max_fanout,
            ],
        ).fetchall()
        out: list[GraphEdge] = []
        for link_id, path, version_id, profile_id in rows:
            feature = self._feature_node(
                str(path), int(version_id), int(profile_id), None
            )
            self._add_node(feature)
            out.append(
                self._make_edge(
                    source=feature.id,
                    target=node.id,
                    relation="member",
                    confidence="INFERRED",
                    source_table="doc_feature_node_links",
                    source_id=int(link_id),
                    version_id=int(version_id),
                    profile_id=int(profile_id),
                    evidence={"node_path": str(path)},
                )
            )
        return out

    def _feature_neighbors(self, node: GraphNode) -> list[GraphEdge]:
        if node.version_id is None or node.profile_id is None:
            return []
        tenant, params = self._tenant_sql("feature_nodes")
        profile = self._profile_sql()
        out: list[GraphEdge] = []
        row = self._conn.execute(
            "SELECT id, parent FROM feature_nodes"
            f" WHERE path = ? AND {self._version_sql()} AND {profile}{tenant} LIMIT 1",
            [node.label, *self._version_ids, *self._profile_ids, *params],
        ).fetchone()
        if row is not None and str(row[1] or ""):
            parent = self._feature_node(
                str(row[1]), node.version_id, node.profile_id, int(row[0])
            )
            self._add_node(parent)
            out.append(
                self._make_edge(
                    source=parent.id,
                    target=node.id,
                    relation="parent",
                    confidence="INFERRED",
                    source_table="feature_nodes",
                    source_id=int(row[0]),
                    version_id=node.version_id,
                    profile_id=node.profile_id,
                    evidence={"parent": str(row[1])},
                )
            )
        rows = self._conn.execute(
            "SELECT id, api_entity_id FROM doc_feature_node_links"
            f" WHERE node_path = ? AND {self._version_sql()} AND {profile}{tenant}"
            " LIMIT ?",
            [
                node.label,
                *self._version_ids,
                *self._profile_ids,
                *params,
                self._max_fanout,
            ],
        ).fetchall()
        for link_id, api_id in rows:
            row_data = self._api_row(int(api_id), node.version_id)
            if row_data is None:
                continue
            api = self._api_node(row_data)
            self._add_node(api)
            out.append(
                self._make_edge(
                    source=node.id,
                    target=api.id,
                    relation="member",
                    confidence="INFERRED",
                    source_table="doc_feature_node_links",
                    source_id=int(link_id),
                    version_id=node.version_id,
                    profile_id=node.profile_id,
                    evidence={"node_path": node.label},
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
        for edge_id, path, version_id in rows:
            file_node = self._file_node(int(version_id), str(path))
            self._add_node(file_node)
            out.append(
                self._make_edge(
                    source=file_node.id,
                    target=node.id,
                    relation="import",
                    confidence="EXTRACTED",
                    source_table="import_edges",
                    source_id=int(edge_id),
                    version_id=int(version_id),
                    profile_id=self.profile_id,
                    evidence={"file": str(path), "target": node.label},
                )
            )
        return out

    def _file_neighbors(self, node: GraphNode) -> list[GraphEdge]:
        if not node.file or node.version_id is None:
            return []
        tenant, params = self._tenant_sql("import_edges")
        rows = self._conn.execute(
            "SELECT id, target, version_id FROM import_edges"
            f" WHERE importer_file = ? AND {self._version_sql()}{tenant} LIMIT ?",
            [node.file, *self._version_ids, *params, self._max_fanout],
        ).fetchall()
        out: list[GraphEdge] = []
        for edge_id, target, version_id in rows:
            api = self._entity_by_name(str(target), int(version_id))
            if api is None:
                continue
            out.append(
                self._make_edge(
                    source=node.id,
                    target=api.id,
                    relation="import",
                    confidence="EXTRACTED",
                    source_table="import_edges",
                    source_id=int(edge_id),
                    version_id=int(version_id),
                    profile_id=self.profile_id,
                    evidence={"file": node.file, "target": str(target)},
                )
            )
        return out

    def _consistency_for_doc(
        self, doc_id: int, version_id: int, profile_id: int
    ) -> list[dict[str, str]]:
        tenant, params = self._tenant_sql("doc_consistency")
        rows = self._conn.execute(
            "SELECT facet, verdict FROM doc_consistency"
            f" WHERE document_id = ? AND {self._version_sql()}"
            f" AND profile_id = ?{tenant} ORDER BY facet LIMIT ?",
            [doc_id, *self._version_ids, profile_id, *params, self._max_fanout],
        ).fetchall()
        return [{"facet": str(r[0]), "verdict": str(r[1])} for r in rows]

    def _doc_neighbors(self, node: GraphNode) -> list[GraphEdge]:
        if node.source_id is None or node.version_id is None:
            return []
        out: list[GraphEdge] = []
        tenant, params = self._tenant_sql("doc_api_links")
        strong, strong_params = _strong_link_sql()
        rows = self._conn.execute(
            "SELECT id, api_entity_id, match_kind, confidence, version_id"
            " FROM doc_api_links"
            f" WHERE document_id = ? AND {self._version_sql()}"
            f" AND {strong}{tenant} ORDER BY confidence DESC LIMIT ?",
            [
                node.source_id,
                *self._version_ids,
                *strong_params,
                *params,
                self._max_fanout,
            ],
        ).fetchall()
        for link_id, api_id, kind, confidence, version_id in rows:
            row_data = self._api_row(int(api_id), int(version_id))
            if row_data is None:
                continue
            api = self._api_node(row_data)
            self._add_node(api)
            out.append(
                self._make_edge(
                    source=node.id,
                    target=api.id,
                    relation="mentions",
                    confidence="EXTRACTED" if kind == "symbol" else "INFERRED",
                    source_table="doc_api_links",
                    source_id=int(link_id),
                    version_id=int(version_id),
                    profile_id=node.profile_id,
                    weight=float(confidence or 1.0),
                    evidence={"match_kind": str(kind)},
                )
            )
        tenant, params = self._tenant_sql("doc_link_edges")
        links = self._conn.execute(
            "SELECT id, dst_url, version_id FROM doc_link_edges"
            f" WHERE src_url = ? AND {self._version_sql()}{tenant} LIMIT ?",
            [node.url, *self._version_ids, *params, self._max_fanout],
        ).fetchall()
        for link_id, dst, version_id in links:
            for target in self._docs_by_url(str(dst)):
                out.append(
                    self._make_edge(
                        source=node.id,
                        target=target.id,
                        relation="doc_link",
                        confidence="INFERRED",
                        source_table="doc_link_edges",
                        source_id=int(link_id),
                        version_id=int(version_id),
                        profile_id=self.profile_id,
                        evidence={"dst_url": str(dst)},
                        evidence_key=f"{node.url}|{dst}",
                    )
                )
        tenant, params = self._tenant_sql("doc_pattern_coverage")
        profile = self._profile_sql()
        covered = self._conn.execute(
            "SELECT id, pattern_hash, pattern, covered_kind, support, version_id,"
            " profile_id FROM doc_pattern_coverage"
            f" WHERE document_id = ? AND {self._version_sql()} AND {profile}{tenant}"
            " ORDER BY support DESC LIMIT ?",
            [
                node.source_id,
                *self._version_ids,
                *self._profile_ids,
                *params,
                self._max_fanout,
            ],
        ).fetchall()
        for row_id, pattern_hash, raw, kind, support, version_id, profile_id in covered:
            try:
                names = [str(n) for n in json.loads(raw) if str(n)] if raw else []
            except ValueError:
                continue
            digest = str(pattern_hash or _pattern_digest(names))
            pattern = self._pattern_node(
                names=names,
                digest=digest,
                source_table="doc_pattern_coverage",
                source_id=int(row_id),
                version_id=int(version_id),
                profile_id=int(profile_id),
                support=int(support or 0),
            )
            self._add_node(pattern)
            out.append(
                self._make_edge(
                    source=node.id,
                    target=pattern.id,
                    relation="covers_pattern",
                    confidence="INFERRED",
                    source_table="doc_pattern_coverage",
                    source_id=int(row_id),
                    version_id=int(version_id),
                    profile_id=int(profile_id),
                    weight=float(support or 1),
                    evidence={
                        "pattern": names,
                        "covered_kind": str(kind),
                        "support": int(support or 0),
                        "consistency": self._consistency_for_doc(
                            int(node.source_id), int(version_id), int(profile_id)
                        ),
                    },
                )
            )
        return out

    def _doc_backlinks(self, node: GraphNode) -> list[GraphEdge]:
        if not node.url or node.version_id is None:
            return []
        tenant, params = self._tenant_sql("doc_link_edges")
        rows = self._conn.execute(
            "SELECT id, src_url, version_id FROM doc_link_edges"
            f" WHERE dst_url = ? AND {self._version_sql()}{tenant} LIMIT ?",
            [node.url, *self._version_ids, *params, self._max_fanout],
        ).fetchall()
        out: list[GraphEdge] = []
        for link_id, src, version_id in rows:
            for source in self._docs_by_url(str(src)):
                out.append(
                    self._make_edge(
                        source=source.id,
                        target=node.id,
                        relation="doc_link",
                        confidence="INFERRED",
                        source_table="doc_link_edges",
                        source_id=int(link_id),
                        version_id=int(version_id),
                        profile_id=self.profile_id,
                        evidence={"src_url": str(src)},
                        evidence_key=f"{src}|{node.url}",
                    )
                )
        return out

    def _pattern_neighbors(self, node: GraphNode) -> list[GraphEdge]:
        if not node.path or node.version_id is None or node.profile_id is None:
            return []
        tenant, params = self._tenant_sql("doc_pattern_coverage")
        profile = self._profile_sql()
        rows = self._conn.execute(
            "SELECT id, document_id, covered_kind, support, version_id, profile_id"
            " FROM doc_pattern_coverage"
            f" WHERE pattern_hash = ? AND {self._version_sql()} AND {profile}{tenant}"
            " ORDER BY support DESC LIMIT ?",
            [
                node.path,
                *self._version_ids,
                *self._profile_ids,
                *params,
                self._max_fanout,
            ],
        ).fetchall()
        out: list[GraphEdge] = []
        for row_id, doc_id, kind, support, version_id, profile_id in rows:
            doc = self._doc_by_id(int(doc_id))
            if doc is None:
                continue
            out.append(
                self._make_edge(
                    source=doc.id,
                    target=node.id,
                    relation="covers_pattern",
                    confidence="INFERRED",
                    source_table="doc_pattern_coverage",
                    source_id=int(row_id),
                    version_id=int(version_id),
                    profile_id=int(profile_id),
                    weight=float(support or 1),
                    evidence={
                        "covered_kind": str(kind),
                        "support": int(support or 0),
                    },
                )
            )
        return out

    def _build_snapshot(self, max_edges: int) -> _AdjacencySnapshot | None:
        builder = _SnapshotView(self, max_edges)
        try:
            return builder.build()
        finally:
            builder.close()

    def neighbors(self, node: GraphNode, *, direction: Direction) -> list[GraphEdge]:
        """Return edges adjacent to one node through the active provider."""
        if self._snapshot is None:
            return self._sql_neighbors(node, direction=direction)
        if not self._budget.take():
            return []
        records: list[_EdgeRecord] = []
        if direction in ("forward", "both"):
            records.extend(self._snapshot.forward.get(node.id, ()))
        if direction in ("backward", "both"):
            records.extend(self._snapshot.backward.get(node.id, ()))
        variants: dict[str, list[_NodeRecord]] = {}
        edges: list[GraphEdge] = []
        for record in records:
            edges.append(_unpack_edge(record))
            for node_id, records_for_node in record.nodes:
                variants.setdefault(node_id, []).extend(records_for_node)
        for node_id, node_variants in variants.items():
            for node_record in node_variants:
                self._add_node(_unpack_node(node_record))
        return edges

    def _sql_neighbors(
        self, node: GraphNode, *, direction: Direction
    ) -> list[GraphEdge]:
        """Derive adjacency from fact tables without any cached projection."""
        out: list[GraphEdge] = []
        if node.source_table == "api_entities":
            if direction in ("forward", "both"):
                out.extend(self._api_neighbors(node, outbound=True))
            if direction in ("backward", "both"):
                out.extend(self._api_neighbors(node, outbound=False))
            return out
        if node.source_table == "documents":
            if direction in ("forward", "both"):
                out.extend(self._doc_neighbors(node))
            if direction in ("backward", "both"):
                out.extend(self._doc_backlinks(node))
            return out
        if node.kind == "file":
            return (
                self._file_neighbors(node) if direction in ("forward", "both") else []
            )
        if node.source_table == "api_clusters":
            return (
                self._cluster_neighbors(node)
                if direction in ("forward", "both")
                else []
            )
        if node.source_table == "feature_nodes":
            edges = self._feature_neighbors(node)
            if direction in ("forward", "both"):
                out.extend(edge for edge in edges if edge.source == node.id)
            if direction in ("backward", "both"):
                out.extend(edge for edge in edges if edge.target == node.id)
            return out
        if node.source_table == "doc_pattern_coverage":
            return (
                self._pattern_neighbors(node)
                if direction in ("backward", "both")
                else []
            )
        return []

    @staticmethod
    def _rank(edge: GraphEdge) -> tuple[int, float, str]:
        priority = (
            10 if edge.relation in {"call", "inherit", "mentions", "import"} else 1
        )
        return (priority, edge.weight, edge.id)

    @staticmethod
    def _target_of(edge: GraphEdge, node_id: str) -> str:
        return edge.target if edge.source == node_id else edge.source

    def _accept(self, edge: GraphEdge, relation_types: frozenset[str] | None) -> bool:
        if edge.relation not in GRAPH_RELATIONS:
            return False
        if relation_types is not None and edge.relation not in relation_types:
            return False
        return not (edge.confidence == "AMBIGUOUS" and not self._include_ambiguous)

    def _neighbor_candidates(
        self,
        node: GraphNode,
        *,
        direction: Direction,
        wanted: frozenset[str] | None,
    ) -> list[GraphEdge]:
        before = dict(self._nodes)
        edges = [
            edge
            for edge in self.neighbors(node, direction=direction)
            if self._accept(edge, wanted)
        ]
        added = {key: value for key, value in self._nodes.items() if key not in before}
        self._nodes = before
        for edge in edges:
            for node_id in (edge.source, edge.target):
                if node_id in added:
                    self._add_node(added[node_id])
        return edges

    def bfs(
        self,
        root: GraphNode,
        *,
        depth: int,
        direction: Direction = "both",
        relation_types: list[str] | None = None,
    ) -> GraphSubgraph:
        """Bounded breadth-first expansion from one seed node."""
        self._open_budget()
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
            for edge in self._neighbor_candidates(
                node, direction=direction, wanted=wanted
            ):
                other = self._target_of(edge, node_id)
                if other == node_id:
                    continue
                key = (other, edge.relation)
                current = candidates.get(key)
                if current is None or self._rank(edge) > self._rank(current):
                    candidates[key] = edge
            if len(candidates) > self._max_fanout:
                truncated = True
            for edge in sorted(candidates.values(), key=self._rank, reverse=True)[
                : self._max_fanout
            ]:
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
                        version_id=edge.version_id,
                        profile_id=edge.profile_id,
                        tenant_id=edge.tenant_id,
                        confidence=edge.confidence,
                        evidence_ref=edge.evidence_ref,
                    )
                    self._add_node(neighbor)
                if other not in visited:
                    visited.add(other)
                    frontier.append((other, hop + 1))
        keep = {root.id}
        for edge in self._edges.values():
            keep.update((edge.source, edge.target))
        self._close_budget()
        return self._result(truncated, keep_nodes=keep)

    def shortest_path(
        self,
        start: GraphNode,
        end: GraphNode,
        *,
        depth: int,
        direction: Direction = "both",
        relation_types: list[str] | None = None,
    ) -> GraphSubgraph:
        """Bounded shortest path using the requested edge direction."""
        self._open_budget()
        wanted = frozenset(relation_types) if relation_types else None
        self._add_node(start)
        self._add_node(end)
        if start.id == end.id:
            self._close_budget()
            return self._result(False)
        frontier: list[tuple[str, int]] = [(start.id, 0)]
        seen = {start.id}
        parents: dict[str, tuple[str, GraphEdge]] = {}
        truncated = False
        while frontier:
            node_id, hop = frontier.pop(0)
            if hop >= max(0, int(depth)):
                continue
            node = self._nodes.get(node_id)
            if node is None:
                continue
            candidates: dict[str, GraphEdge] = {}
            for edge in self._neighbor_candidates(
                node, direction=direction, wanted=wanted
            ):
                other = self._target_of(edge, node_id)
                current = candidates.get(other)
                if current is None or self._rank(edge) > self._rank(current):
                    candidates[other] = edge
            if len(candidates) > self._max_fanout:
                truncated = True
            ordered = sorted(
                candidates.items(), key=lambda item: self._rank(item[1]), reverse=True
            )[: self._max_fanout]
            for target, edge in ordered:
                if target not in seen and len(self._nodes) >= self._max_nodes:
                    truncated = True
                    continue
                self._add_edge(edge)
                if target not in seen:
                    seen.add(target)
                    parents[target] = (node_id, edge)
                    if target == end.id:
                        self._close_budget()
                        return self._path_result(start.id, end.id, parents, truncated)
                    frontier.append((target, hop + 1))
        self._close_budget()
        return GraphSubgraph(truncated=truncated or self._truncated, found=False)

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

    def _result(
        self, truncated: bool, *, keep_nodes: set[str] | None = None
    ) -> GraphSubgraph:
        nodes = [
            node
            for node in self._nodes.values()
            if keep_nodes is None or node.id in keep_nodes
        ]
        return GraphSubgraph(
            nodes=sorted(nodes, key=lambda n: n.id),
            edges=sorted(self._edges.values(), key=lambda e: (-e.weight, e.id)),
            truncated=bool(truncated or self._truncated),
        )


class _SnapshotOverflow(Exception):
    """Raised when a snapshot would exceed its configured edge capacity."""


def _copy_graph_facts(
    conn: Any,
    version_ids: list[int],
    profile_ids: list[int],
    tenant_id: str | None,
) -> tuple[sqlite3.Connection | None, int]:
    """Copy scoped contribution tables once into an indexed in-memory database.

    Derivation SQL keeps running unchanged, but only against this snapshot copy;
    the source fact tables are scanned once per contribution table. Backends
    without a SQLite file path return ``None`` so the caller uses SQL directly.
    """
    raw = getattr(conn, "raw", conn)
    if not isinstance(raw, sqlite3.Connection):
        return None, 0
    memory: sqlite3.Connection | None = None
    try:
        rows = raw.execute("PRAGMA database_list").fetchall()
        path = next(
            (str(row[2]) for row in rows if str(row[1]) == "main" and row[2]), ""
        )
        if not path:
            return None, 0
        memory = sqlite3.connect(":memory:")
        memory.execute("ATTACH DATABASE ? AS graph_src", (path,))
        scans = 0
        for table in _SNAPSHOT_TABLES:
            ddl = memory.execute(
                "SELECT sql FROM graph_src.sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if ddl is None or not ddl[0]:
                continue
            memory.execute(str(ddl[0]))
            columns = [
                str(row[1])
                for row in memory.execute(f"PRAGMA graph_src.table_info({table})")
            ]
            if not columns:
                continue
            conditions: list[str] = []
            params: list[Any] = []
            if "version_id" in columns:
                conditions.append(
                    f"version_id IN ({','.join('?' for _ in version_ids)})"
                )
                params.extend(version_ids)
            if "profile_id" in columns:
                conditions.append(
                    f"profile_id IN ({','.join('?' for _ in profile_ids)})"
                )
                params.extend(profile_ids)
            if tenant_id is not None and "tenant_id" in columns:
                conditions.append("tenant_id = ?")
                params.append(tenant_id)
            where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
            names = ",".join(columns)
            memory.execute(
                f"INSERT INTO {table}({names})"
                f" SELECT {names} FROM graph_src.{table}{where}",
                params,
            )
            indexes = memory.execute(
                "SELECT sql FROM graph_src.sqlite_master"
                " WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                (table,),
            ).fetchall()
            for index in indexes:
                memory.execute(str(index[0]))
            scans += 1
        return memory, scans
    except Exception as exc:  # noqa: BLE001 -- any copy failure falls back to SQL
        logger.debug(
            "graph snapshot fact copy failed reason=%s error=%s",
            type(exc).__name__,
            exc,
        )
        if memory is not None:
            memory.close()
        return None, 0


class _SnapshotView(GraphView):
    """Scan contribution tables once and record edges through SQL derivations."""

    def __init__(self, source: GraphView, max_edges: int) -> None:
        facts, scans = _copy_graph_facts(
            source._raw_conn,
            source._version_ids,
            source._profile_ids,
            source._tenant_id,
        )
        self._facts = facts
        self._scans = scans
        super().__init__(
            facts or source._raw_conn,
            source._version_ids,
            tenant_id=source._tenant_id,
            profile_ids=source._profile_ids,
            profile_name=source._profile_name,
            max_nodes=source._max_nodes,
            max_fanout=max(source._max_fanout, int(max_edges) + 1),
            pattern_limit=source._pattern_limit,
            query_budget=1,
            include_ambiguous=source._include_ambiguous,
        )
        self._budget.active = False
        self._max_edges = max(1, int(max_edges))
        self._records: dict[tuple[str, Direction], _EdgeRecord] = {}
        self._seeds: dict[str, GraphNode] = {}

    def close(self) -> None:
        if self._facts is not None:
            self._facts.close()

    def _scan(self, sql: str, params: list[Any] | None = None) -> list[tuple]:
        return [
            tuple(row)
            for row in self._raw_conn.execute(sql, tuple(params or ())).fetchall()
        ]

    def _seed(self, node: GraphNode) -> None:
        self._seeds.setdefault(node.id, node)

    def neighbors(self, node: GraphNode, *, direction: Direction) -> list[GraphEdge]:
        before = dict(self._nodes)
        edges = self._sql_neighbors(node, direction=direction)
        added = {key: value for key, value in self._nodes.items() if key not in before}
        for edge in edges:
            variants = tuple(
                (node_id, (_pack_node(added[node_id]),))
                for node_id in sorted({edge.source, edge.target})
                if node_id in added
            )
            record = _pack_edge(edge, variants, direction=direction, anchor=node.id)
            key = (edge.id, direction)
            previous = self._records.get(key)
            self._records[key] = (
                record if previous is None else _merge_edge_records(previous, record)
            )
        if len(self._records) > self._max_edges:
            raise _SnapshotOverflow
        return edges

    def _load_seeds(self) -> None:
        tenant, params = self._tenant_sql("api_entities")
        for row in self._scan(
            "SELECT id, qualified_name, kind, file, version_id, line_start, line_end"
            " FROM api_entities"
            f" WHERE {self._version_sql()}{tenant} ORDER BY id",
            [*self._version_ids, *params],
        ):
            self._seed(self._api_node(row))

        tenant, params = self._tenant_sql("documents")
        for row in self._scan(
            "SELECT id, url, title, path, version_id FROM documents"
            f" WHERE {self._version_sql()}{tenant} ORDER BY id",
            [*self._version_ids, *params],
        ):
            self._seed(self._doc_node(row))

        tenant, params = self._tenant_sql("api_clusters")
        profile = self._profile_sql()
        for row in self._scan(
            "SELECT id, cluster_id, size, version_id, profile_id FROM api_clusters"
            f" WHERE {self._version_sql()} AND {profile}{tenant} ORDER BY id",
            [*self._version_ids, *self._profile_ids, *params],
        ):
            self._seed(
                self._cluster_node(
                    cluster_id=int(row[1]),
                    source_id=int(row[0]),
                    version_id=int(row[3]),
                    profile_id=int(row[4]),
                    size=int(row[2]),
                )
            )

        tenant, params = self._tenant_sql("feature_nodes")
        for row in self._scan(
            "SELECT id, path, version_id, profile_id FROM feature_nodes"
            f" WHERE {self._version_sql()} AND {profile}{tenant} ORDER BY id",
            [*self._version_ids, *self._profile_ids, *params],
        ):
            self._seed(
                self._feature_node(str(row[1]), int(row[2]), int(row[3]), int(row[0]))
            )

        tenant, params = self._tenant_sql("doc_pattern_coverage")
        for row in self._scan(
            "SELECT id, pattern_hash, pattern, support, version_id, profile_id"
            " FROM doc_pattern_coverage"
            f" WHERE {self._version_sql()} AND {profile}{tenant} ORDER BY id",
            [*self._version_ids, *self._profile_ids, *params],
        ):
            try:
                names = (
                    [str(name) for name in json.loads(row[2]) if str(name)]
                    if row[2]
                    else []
                )
            except TypeError, ValueError:
                names = []
            self._seed(
                self._pattern_node(
                    names=names,
                    digest=str(row[1] or _pattern_digest(names)),
                    source_table="doc_pattern_coverage",
                    source_id=int(row[0]),
                    version_id=int(row[4]),
                    profile_id=int(row[5]),
                    support=int(row[3] or 0),
                )
            )

        tenant, params = self._tenant_sql("call_instances")
        for row in self._scan(
            "SELECT DISTINCT caller_file, version_id FROM call_instances"
            f" WHERE {self._version_sql()} AND in_target_api = 1{tenant}"
            " ORDER BY caller_file",
            [*self._version_ids, *params],
        ):
            self._seed(self._file_node(int(row[1]), str(row[0])))
        tenant, params = self._tenant_sql("import_edges")
        for row in self._scan(
            "SELECT DISTINCT importer_file, version_id FROM import_edges"
            f" WHERE {self._version_sql()}{tenant} ORDER BY importer_file",
            [*self._version_ids, *params],
        ):
            self._seed(self._file_node(int(row[1]), str(row[0])))

    def build(self) -> _AdjacencySnapshot:
        self._load_seeds()
        pending = list(self._seeds.values())
        processed: set[str] = set()
        while pending:
            node = pending.pop(0)
            if node.id in processed:
                continue
            processed.add(node.id)
            for direction in ("forward", "backward"):
                self._nodes = {node.id: node}
                self.neighbors(node, direction=direction)
            for node_id, discovered in self._nodes.items():
                if node_id not in self._seeds:
                    self._seeds[node_id] = discovered
                    pending.append(discovered)
        return _AdjacencySnapshot(self._records, table_scans=self._scans)
