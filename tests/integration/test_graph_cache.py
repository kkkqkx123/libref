"""Differential and invalidation tests for the derived graph adjacency cache."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from libref.services.graph_query import GraphQueryService
from libref.store.graph_cache import (
    CacheKey,
    GraphAdjacencyCache,
    clear_graph_adjacency_caches,
    get_graph_adjacency_cache,
    graph_cache_context,
)
from tests.conftest import make_settings
from tests.integration.test_graph_query import _GOLD, _seed

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _isolate_graph_caches():
    clear_graph_adjacency_caches()
    yield
    clear_graph_adjacency_caches()


class _CountingConnection:
    """Record fact SQL while keeping the session surface transparent."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self.sql: list[str] = []

    @property
    def raw(self) -> Any:
        return self._raw

    def execute(self, sql: str, params: Any = ()) -> Any:
        self.sql.append(sql)
        return self._raw.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


class _NoWatermarkConnection:
    """Connection shape whose backend cannot supply a fact watermark."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def execute(self, sql: str, params: Any = ()) -> Any:
        return self._raw.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        if name == "total_changes":
            raise AttributeError(name)
        return getattr(self._raw, name)


def _service(
    conn: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    cache: bool = False,
    **overrides: Any,
) -> GraphQueryService:
    settings = make_settings(
        monkeypatch,
        tmp_path,
        graph_max_depth=2,
        graph_cache_enabled=cache,
        **overrides,
    )
    return GraphQueryService(conn, settings)


def _query(
    conn: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    cache: bool = False,
    **options: Any,
) -> dict[str, Any]:
    options.setdefault("tenant_id", "default")
    settings_options = {
        key: options.pop(key) for key in list(options) if key.startswith("graph_")
    }
    return _service(conn, monkeypatch, tmp_path, cache=cache, **settings_options).query(
        options.pop("mode", "neighbors"),
        options.pop("entity", "pkg.client.Client"),
        library="graphlib",
        version="1.0",
        **options,
    )


def _comparable(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "nodes": result["nodes"],
        "edges": result["edges"],
        "truncated": result["truncated"],
    }


def test_snapshot_builder_scans_tables_without_source_derivation_reads(
    conn, monkeypatch, tmp_path
) -> None:
    _seed(conn)
    counting = _CountingConnection(conn)
    cache = get_graph_adjacency_cache(50000, 300.0)
    scans_before = cache.stats["table_scans"]
    result = _query(
        counting,
        monkeypatch,
        tmp_path,
        cache=True,
        direction="both",
        depth=2,
    )
    assert result["edges"]
    assert cache.stats["table_scans"] - scans_before > 0
    assert not any("FROM call_instances ci" in sql for sql in counting.sql)


def test_cache_identity_is_the_database_file(conn, tmp_path) -> None:
    context = graph_cache_context(conn)
    assert context is not None
    assert context[1] == str(tmp_path / "test.db")


def test_cached_results_match_sql_gold_paths(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    cases = _GOLD["cases"]
    plain = _service(conn, monkeypatch, tmp_path).audit(
        cases, library="graphlib", version="1.0", tenant_id="default"
    )
    cached = _service(conn, monkeypatch, tmp_path, cache=True).audit(
        cases, library="graphlib", version="1.0", tenant_id="default"
    )
    assert [case["passed"] for case in plain["cases"]] == [
        case["passed"] for case in cached["cases"]
    ]
    assert all(case["passed"] for case in cached["cases"])
    assert [case["actual_nodes"] for case in plain["cases"]] == [
        case["actual_nodes"] for case in cached["cases"]
    ]
    assert [case["actual_relations"] for case in plain["cases"]] == [
        case["actual_relations"] for case in cached["cases"]
    ]
    assert [case["evidence_refs"] for case in plain["cases"]] == [
        case["evidence_refs"] for case in cached["cases"]
    ]

    options = {
        "entity": "pkg.client.Client.send",
        "direction": "both",
        "depth": 2,
    }
    sql_result = _query(conn, monkeypatch, tmp_path, **options)
    cache_result = _query(conn, monkeypatch, tmp_path, cache=True, **options)
    assert _comparable(sql_result) == _comparable(cache_result)
    for case in cases:
        options = {
            "mode": case["mode"],
            "entity": case["entity"],
            "target": case.get("target"),
            "direction": case.get("direction", "both"),
            "depth": case.get("depth"),
            "relation_types": case.get("relation_types"),
        }
        plain_result = _query(conn, monkeypatch, tmp_path, **options)
        cached_result = _query(conn, monkeypatch, tmp_path, cache=True, **options)
        assert _comparable(plain_result) == _comparable(cached_result)
    assert get_graph_adjacency_cache(50000, 300.0).stats["builds"] >= 1


def test_pattern_pairs_merge_by_support_and_scan_once(
    conn, monkeypatch, tmp_path
) -> None:
    _seed(conn)
    conn.executemany(
        "INSERT INTO seq_patterns"
        " (id, library_id, version_id, profile_id, pattern, support, tenant_id)"
        " VALUES (?, 1, 1, 1, ?, ?, 'default')",
        [
            (
                3,
                json.dumps(
                    [
                        "pkg.client.Client",
                        "pkg.client.Client.send",
                        "pkg.client.Client.close",
                    ]
                ),
                9,
            ),
            (
                4,
                json.dumps(
                    ["pkg.client.Client", "pkg.client.Client.send", "pkg.helper.retry"]
                ),
                9,
            ),
        ],
    )
    conn.commit()
    counting = _CountingConnection(conn)
    result = _query(
        counting,
        monkeypatch,
        tmp_path,
        direction="forward",
        depth=2,
        relation_types=["pattern_next"],
    )
    edges = [edge for edge in result["edges"] if edge["relation"] == "pattern_next"]
    pair = [
        edge
        for edge in edges
        if edge["target"]
        in {
            node["id"]
            for node in result["nodes"]
            if node["label"] == "pkg.client.Client.send"
        }
    ]
    assert len(pair) == 1
    assert pair[0]["weight"] == 9
    assert pair[0]["evidence"]["pattern_id"] == 3
    assert pair[0]["evidence"]["support"] == 9
    assert pair[0]["evidence"]["position"] == 0
    assert sum("FROM seq_patterns" in sql for sql in counting.sql) == 1
    assert sum("qualified_name IN" in sql for sql in counting.sql) == 1

    cached = _query(
        conn,
        monkeypatch,
        tmp_path,
        cache=True,
        direction="forward",
        depth=2,
        relation_types=["pattern_next"],
    )
    assert [edge["id"] for edge in cached["edges"]] == [edge["id"] for edge in edges]


def test_new_fact_invalidates_snapshot(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    builds_before = get_graph_adjacency_cache(50000, 300.0).stats["builds"]
    before = _query(
        conn,
        monkeypatch,
        tmp_path,
        cache=True,
        entity="pkg.client.Client",
        direction="forward",
        depth=1,
        relation_types=["call"],
    )
    assert all(node["label"] != "pkg.helper.retry" for node in before["nodes"])
    conn.execute(
        "INSERT INTO call_instances"
        " (id, call_uid, callee_entity_id, callee_name, repo, caller_file,"
        " line_start, line_end, confidence, version_id, caller_entity_id,"
        " in_target_api, tenant_id)"
        " VALUES (6, 'call-6', 3, 'pkg.helper.retry', 'repo', 'pkg/new.py',"
        " 1, 2, 'EXTRACTED', 1, 1, 1, 'default')"
    )
    conn.commit()
    after = _query(
        conn,
        monkeypatch,
        tmp_path,
        cache=True,
        entity="pkg.client.Client",
        direction="forward",
        depth=1,
        relation_types=["call"],
    )
    assert "pkg.helper.retry" in {node["label"] for node in after["nodes"]}
    assert get_graph_adjacency_cache(50000, 300.0).stats["builds"] - builds_before == 2


def test_cache_hit_still_honors_query_budget(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    warm = _query(conn, monkeypatch, tmp_path, cache=True, direction="both", depth=2)
    assert warm["edges"]
    result = _query(
        conn,
        monkeypatch,
        tmp_path,
        cache=True,
        direction="both",
        depth=2,
        graph_query_budget=1,
    )
    assert result["truncated"] is True
    plain = _query(
        conn,
        monkeypatch,
        tmp_path,
        direction="both",
        depth=2,
        graph_query_budget=1,
    )
    assert plain["truncated"] is True


def test_capacity_and_watermark_fallback_keep_full_results(
    conn, monkeypatch, tmp_path
) -> None:
    _seed(conn)
    options = {"entity": "pkg.client.Client.send", "direction": "both", "depth": 2}
    expected = _query(conn, monkeypatch, tmp_path, **options)

    capped = _query(
        conn,
        monkeypatch,
        tmp_path,
        cache=True,
        graph_cache_max_edges=1,
        **options,
    )
    assert _comparable(capped) == _comparable(expected)
    assert get_graph_adjacency_cache(1, 300.0).stats["fallbacks"] >= 1

    no_watermark = _NoWatermarkConnection(conn)
    fallback = _query(no_watermark, monkeypatch, tmp_path, cache=True, **options)
    assert _comparable(fallback) == _comparable(expected)


def test_concurrent_acquire_builds_one_snapshot() -> None:
    cache = GraphAdjacencyCache(100, 300.0)
    key = CacheKey("sqlite", "db", "tenant", 1, (1,), "usage", False, 200, "w")
    started = threading.Barrier(4)
    builds: list[int] = []

    def build() -> SimpleNamespace:
        builds.append(1)
        time.sleep(0.05)
        return SimpleNamespace(edge_count=2)

    def acquire() -> None:
        started.wait()
        lease = cache.acquire(key, build)
        assert lease is not None
        time.sleep(0.01)
        lease.release()

    threads = [threading.Thread(target=acquire) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(builds) == 1
    assert cache.stats["hits"] == 3
    assert cache.stats["builds"] == 1
