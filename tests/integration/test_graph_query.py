"""Graph query integration tests over seeded source-of-truth relations."""

from __future__ import annotations

import json

import pytest

from libref.services.graph_query import GraphQueryService
from tests.conftest import make_settings, seed_library_version

pytestmark = pytest.mark.integration


def _seed(conn) -> None:
    seed_library_version(conn, "graphlib", "1.0")
    conn.executemany(
        "INSERT INTO api_entities"
        " (id, entity_uid, library_id, version_id, qualified_name, kind, bases)"
        " VALUES (?, ?, 1, 1, ?, ?, '[]')",
        [
            (1, "a", "pkg.client.Client", "class"),
            (2, "b", "pkg.client.Client.send", "method"),
            (3, "c", "pkg.helper.retry", "function"),
        ],
    )
    conn.executemany(
        "INSERT INTO call_instances"
        " (id, call_uid, callee_entity_id, callee_name, repo, caller_file,"
        " line_start, line_end, confidence, version_id, caller_entity_id,"
        " in_target_api, tenant_id)"
        " VALUES (?, ?, ?, ?, 'repo', ?, ?, ?, ?, 1, ?, 1, 'default')",
        [
            (
                1,
                "call-1",
                2,
                "pkg.client.Client.send",
                "pkg/app.py",
                10,
                12,
                "EXTRACTED",
                1,
            ),
            (
                2,
                "call-2",
                1,
                "pkg.client.Client",
                "pkg/retry.py",
                20,
                21,
                "AMBIGUOUS",
                3,
            ),
        ],
    )
    conn.execute(
        "INSERT INTO pages(id, version_id, url) VALUES(1, 1, 'https://g.example/api')"
    )
    conn.executemany(
        "INSERT INTO documents"
        " (id, library_id, version_id, page_id, url, content, metadata, sort_order)"
        " VALUES (?, 1, 1, 1, ?, ?, ?, ?)",
        [
            (1, "https://g.example/api", "send body", json.dumps({"title": "API"}), 0),
            (
                2,
                "https://g.example/weak",
                "weak body",
                json.dumps({"title": "Weak"}),
                1,
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO doc_api_links"
        " (id, library_id, version_id, document_id, api_entity_id, match_kind,"
        " confidence, evidence)"
        " VALUES (?, 1, 1, ?, ?, ?, 0.9, '{}')",
        [
            (1, 1, 2, "symbol"),
            (2, 2, 2, "name_fuzzy"),
        ],
    )
    conn.execute(
        "INSERT INTO seq_patterns"
        " (id, library_id, version_id, profile_id, pattern, support, tenant_id)"
        " VALUES (1, 1, 1, 1, ?, 7, 'default')",
        (json.dumps(["pkg.client.Client", "pkg.client.Client.send"]),),
    )
    conn.execute(
        "INSERT INTO api_clusters"
        " (id, library_id, version_id, profile_id, cluster_id, member, size,"
        " representative, tenant_id)"
        " VALUES (1, 1, 1, 1, 4, 'pkg.client.Client.send', 2, 'send', 'default')"
    )
    conn.execute(
        "INSERT INTO import_edges"
        " (id, library_id, version_id, importer_file, alias, target, tenant_id)"
        " VALUES (1, 1, 1, 'pkg/entry.py', 'Client', 'pkg.client.Client', 'default')"
    )
    conn.commit()


def _service(conn, monkeypatch, tmp_path) -> GraphQueryService:
    return GraphQueryService(
        conn, make_settings(monkeypatch, tmp_path, graph_max_depth=2)
    )


def _call(conn, monkeypatch, tmp_path, **kwargs) -> dict:
    return _service(conn, monkeypatch, tmp_path).query(
        kwargs.pop("mode", "neighbors"),
        kwargs.pop("entity", "pkg.client.Client"),
        library="graphlib",
        version="1.0",
        **kwargs,
    )


def test_neighbors_keep_strong_edges_and_bounded_direction(
    conn, monkeypatch, tmp_path
) -> None:
    _seed(conn)
    result = _call(conn, monkeypatch, tmp_path, direction="forward")
    relations = {edge["relation"] for edge in result["edges"]}
    assert "call" in relations
    assert "pattern_member" in relations
    assert "mentions" not in relations
    target_labels = {node["label"] for node in result["nodes"]}
    assert "pkg.client.Client.send" in target_labels
    assert "pkg/client.py" not in target_labels

    backward = _call(
        conn,
        monkeypatch,
        tmp_path,
        entity="pkg.client.Client.send",
        direction="backward",
    )
    labels = {node["label"] for node in backward["nodes"]}
    assert "API" in labels
    assert "Weak" not in labels
    assert "cluster:4" in labels
    assert "pkg.client.Client" in labels
    assert "pkg/entry.py" in labels


def test_path_uses_only_graph_edges(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    result = _call(
        conn,
        monkeypatch,
        tmp_path,
        mode="path",
        entity="pkg.client.Client",
        target="pkg.client.Client.send",
    )
    assert result["found"] is True
    assert [edge["relation"] for edge in result["edges"]] == ["call"]
    missing = _call(
        conn,
        monkeypatch,
        tmp_path,
        mode="path",
        entity="pkg.client.Client",
        target="pkg.helper.retry",
        relation_types=["import"],
    )
    assert missing["found"] is False


def test_usage_context_reuses_mining_reads(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    result = _call(
        conn,
        monkeypatch,
        tmp_path,
        mode="usage",
        entity="pkg.client.Client.send",
    )
    assert result["mode"] == "usage"
    assert "related_apis" in result["usage"]
    assert "patterns" in result["usage"]
    assert "arg_shapes" in result["usage"]
    assert any(node["label"] == "API" for node in result["documents"])
    assert all(node["label"] != "Weak" for node in result["documents"])


def test_ambiguous_edges_are_diagnostic_by_default(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    plain = _call(
        conn,
        monkeypatch,
        tmp_path,
        entity="pkg.client.Client",
        direction="backward",
        relation_types=["call"],
    )
    assert all(node["label"] != "pkg.helper.retry" for node in plain["nodes"])
    expanded = _call(
        conn,
        monkeypatch,
        tmp_path,
        entity="pkg.client.Client",
        direction="backward",
        relation_types=["call"],
        include_ambiguous=True,
    )
    assert any(node["label"] == "pkg.helper.retry" for node in expanded["nodes"])


def test_web_graph_endpoint_uses_the_same_service(monkeypatch, tmp_path) -> None:
    from fastapi.testclient import TestClient

    from libref.db import apply_migrations, open_db
    from libref.web import build_web_app

    web_conn = open_db(tmp_path / "graph-web.db", check_same_thread=False)
    apply_migrations(web_conn)
    _seed(web_conn)
    monkeypatch.chdir(tmp_path)
    settings = make_settings(monkeypatch, tmp_path, auth_mode="dev")
    client = TestClient(build_web_app(web_conn, settings))
    response = client.get(
        "/graph",
        params={
            "entity": "pkg.client.Client",
            "library": "graphlib",
            "version": "1.0",
            "direction": "forward",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "neighbors"
    assert any(edge["relation"] == "call" for edge in payload["edges"])
    rejected = client.get(
        "/graph",
        params={"entity": "pkg.client.Client", "library": "graphlib"},
    )
    assert rejected.status_code == 400
    web_conn.close()


def test_graph_query_requires_explicit_version(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    with pytest.raises(ValueError, match="library and version are required"):
        _service(conn, monkeypatch, tmp_path).query(
            "neighbors", "pkg.client.Client", library="graphlib"
        )
