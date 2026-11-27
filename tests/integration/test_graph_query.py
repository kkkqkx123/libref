"""Graph query integration tests over seeded source-of-truth relations."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from libref.services.graph_query import (
    GRAPH_DIRECTIONS,
    GRAPH_MODES,
    GraphQueryService,
)
from libref.store.graph import GRAPH_RELATIONS
from tests.conftest import make_settings, seed_library_version

pytestmark = pytest.mark.integration

_GOLD = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "graph_gold.json").read_text(
        encoding="utf-8"
    )
)
_PG_URL = os.environ.get("LIBREF_TEST_POSTGRES_URL", "")


def _fresh_postgres(url: str) -> None:
    import psycopg
    from psycopg import sql

    parsed = psycopg.conninfo.conninfo_to_dict(url)
    dbname = str(parsed.get("dbname") or "postgres")
    admin = dict(parsed, dbname="postgres")
    with psycopg.connect(**admin, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = %s AND pid <> pg_backend_pid()",
            (dbname,),
        )
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(dbname))
        )
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))


class _PostgresSettings:
    metadata_backend = "postgres"
    fulltext_backend = "postgres"
    postgres_url = _PG_URL
    postgres_pool_size = 2
    postgres_timeout = 5.0


def _seed(conn) -> None:
    seed_library_version(conn, "graphlib", "1.0")
    conn.executemany(
        "INSERT INTO api_entities"
        " (id, entity_uid, library_id, version_id, qualified_name, kind, bases)"
        " VALUES (?, ?, 1, 1, ?, ?, ?)",
        [
            (1, "a", "pkg.client.Client", "class", "[]"),
            (2, "b", "pkg.client.Client.send", "method", "[]"),
            (3, "c", "pkg.helper.retry", "function", "[]"),
            (4, "d", "pkg.client.Client.close", "method", "[]"),
            (5, "e", "pkg.client.SecureClient", "class", '["pkg.client.Client"]'),
        ],
    )
    conn.execute(
        "INSERT INTO mining_profiles(id, version_id, name, source_ids, support_mode,"
        " params, fingerprint, tenant_id)"
        " VALUES(1, 1, 'usage', '[]', 'sample', '{}', 'fp', 'default')"
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
            (
                3,
                "call-3",
                4,
                "pkg.client.Client.close",
                "pkg/app.py",
                14,
                15,
                "EXTRACTED",
                2,
            ),
            (
                4,
                "call-4",
                1,
                "pkg.client.Client",
                "pkg/script.py",
                30,
                31,
                "EXTRACTED",
                None,
            ),
            (
                5,
                "call-5",
                3,
                "pkg.helper.retry",
                "pkg/app.py",
                40,
                41,
                "EXTRACTED",
                1,
            ),
        ],
    )
    conn.execute(
        "INSERT INTO review_status(entity_type, entity_id, status, tenant_id)"
        " VALUES('call_instance', 5, 'conflict', 'default')"
    )
    conn.executemany(
        "INSERT INTO pages(id, version_id, url) VALUES(?, 1, ?)",
        [
            (1, "https://g.example/api"),
            (2, "https://g.example/related"),
        ],
    )
    conn.executemany(
        "INSERT INTO documents"
        " (id, library_id, version_id, page_id, url, content, metadata, sort_order)"
        " VALUES (?, 1, 1, ?, ?, ?, ?, ?)",
        [
            (
                1,
                1,
                "https://g.example/api",
                "send body",
                json.dumps({"title": "API"}),
                0,
            ),
            (
                2,
                1,
                "https://g.example/weak",
                "weak body",
                json.dumps({"title": "Weak"}),
                1,
            ),
            (
                3,
                2,
                "https://g.example/related",
                "related body",
                json.dumps({"title": "Related"}),
                0,
            ),
            (
                4,
                2,
                "https://g.example/related",
                "related detail body",
                json.dumps({"title": "Related Detail"}),
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
            (3, 3, 4, "signature"),
        ],
    )
    conn.executemany(
        "INSERT INTO seq_patterns"
        " (id, library_id, version_id, profile_id, pattern, support, tenant_id)"
        " VALUES (?, 1, 1, 1, ?, ?, 'default')",
        [
            (
                1,
                json.dumps(["pkg.client.Client", "pkg.client.Client.send"]),
                7,
            ),
            (
                2,
                json.dumps(["pkg.client.Client.send", "pkg.client.Client.close"]),
                5,
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO api_clusters"
        " (id, library_id, version_id, profile_id, cluster_id, member, size,"
        " representative, tenant_id)"
        " VALUES (?, 1, 1, 1, 4, ?, 2, 'pkg.client.Client.send', 'default')",
        [
            (1, "pkg.client.Client.send"),
            (2, "pkg.client.Client.close"),
        ],
    )
    conn.execute(
        "INSERT INTO import_edges"
        " (id, library_id, version_id, importer_file, alias, target, tenant_id)"
        " VALUES (1, 1, 1, 'pkg/entry.py', 'Client', 'pkg.client.Client', 'default')"
    )
    conn.execute(
        "INSERT INTO feature_nodes(id, library_id, version_id, profile_id, path,"
        " parent, depth, kind, calls, scopes, tenant_id)"
        " VALUES (1, 1, 1, 1, 'pkg.client.send', 'pkg.client', 2, 'method', 8, 3,"
        " 'default')"
    )
    conn.execute(
        "INSERT INTO doc_feature_node_links(id, library_id, version_id, profile_id,"
        " document_id, api_entity_id, node_path, tenant_id)"
        " VALUES (1, 1, 1, 1, 1, 2, 'pkg.client.send', 'default')"
    )
    conn.execute(
        "INSERT INTO doc_pattern_coverage(id, library_id, version_id, profile_id,"
        " document_id, pattern_hash, pattern, covered_kind, support, tenant_id)"
        " VALUES (1, 1, 1, 1, 1, 'pattern-1', ?, 'prose', 7, 'default')",
        (json.dumps(["pkg.client.Client", "pkg.client.Client.send"]),),
    )
    conn.execute(
        "INSERT INTO doc_consistency(id, library_id, version_id, profile_id,"
        " document_id, facet, verdict, note, evidence_hash, model, prompt_version,"
        " tenant_id)"
        " VALUES (1, 1, 1, 1, 1, 'coverage', 'match', '', 'hash', 'rule', '',"
        " 'default')"
    )
    conn.execute(
        "INSERT INTO doc_link_edges(id, library_id, version_id, src_url, dst_url, kind)"
        " VALUES (1, 1, 1, 'https://g.example/api', 'https://g.example/related',"
        " 'internal')"
    )
    conn.commit()


def _seed_postgres(session) -> None:
    lib_id = session.insert(
        "INSERT INTO libraries(ecosystem, name) VALUES('npm', 'graph-pg')"
    )
    version_id = session.insert(
        "INSERT INTO versions(library_id, version) VALUES(?, '1.0')", (lib_id,)
    )
    caller = session.insert(
        "INSERT INTO api_entities(entity_uid, library_id, version_id, qualified_name,"
        " kind) VALUES(?, ?, ?, ?, ?)",
        ("pg-client", lib_id, version_id, "pkg.Client", "class"),
    )
    callee = session.insert(
        "INSERT INTO api_entities(entity_uid, library_id, version_id, qualified_name,"
        " kind) VALUES(?, ?, ?, ?, ?)",
        ("pg-send", lib_id, version_id, "pkg.Client.send", "method"),
    )
    session.insert(
        "INSERT INTO mining_profiles(version_id, name, support_mode, fingerprint)"
        " VALUES(?, 'usage', 'sample', 'pg')",
        (version_id,),
    )
    session.execute(
        "INSERT INTO call_instances(call_uid, callee_entity_id, callee_name, repo,"
        " caller_file, line_start, line_end, confidence, version_id,"
        " caller_entity_id, in_target_api)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "pg-call",
            callee,
            "pkg.Client.send",
            "pg-repo",
            "pkg/app.py",
            4,
            6,
            "EXTRACTED",
            version_id,
            caller,
            1,
        ),
    )
    page = session.insert(
        "INSERT INTO pages(version_id, url) VALUES(?, 'https://pg.example/api')",
        (version_id,),
    )
    doc = session.insert(
        "INSERT INTO documents(library_id, version_id, page_id, url, content,"
        " metadata, sort_order) VALUES(?, ?, ?, ?, ?, ?::jsonb, ?)",
        (
            lib_id,
            version_id,
            page,
            "https://pg.example/api",
            "API body",
            json.dumps({"title": "API"}),
            0,
        ),
    )
    session.execute(
        "INSERT INTO doc_api_links(library_id, version_id, document_id,"
        " api_entity_id, match_kind, confidence) VALUES(?, ?, ?, ?, ?, ?)",
        (lib_id, version_id, doc, callee, "symbol", 0.9),
    )


def _service(conn, monkeypatch, tmp_path) -> GraphQueryService:
    return GraphQueryService(
        conn, make_settings(monkeypatch, tmp_path, graph_max_depth=2)
    )


def _call(conn, monkeypatch, tmp_path, **kwargs) -> dict:
    kwargs.setdefault("tenant_id", "default")
    kwargs.setdefault("profile", "usage")
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
    result = _call(conn, monkeypatch, tmp_path, direction="forward", depth=1)
    relations = {edge["relation"] for edge in result["edges"]}
    assert "call" in relations
    assert "pattern_next" in relations
    assert "mentions" not in relations
    target_labels = {node["label"] for node in result["nodes"]}
    assert "pkg.client.Client.send" in target_labels
    assert all(edge["source"] == result["seed"]["id"] for edge in result["edges"])

    backward = _call(
        conn,
        monkeypatch,
        tmp_path,
        entity="pkg.client.Client.send",
        direction="backward",
    )
    labels = {node["label"] for node in backward["nodes"]}
    assert "API" in labels
    assert "Related" not in labels
    assert "Weak" not in labels
    assert "cluster:4" in labels
    assert "pkg.client.Client" in labels
    assert "pkg/entry.py" in labels
    assert backward["profile"] == "usage"
    assert backward["tenant_id"] == "default"
    both = _call(
        conn,
        monkeypatch,
        tmp_path,
        entity="pkg.client.Client.send",
        direction="both",
    )
    assert "Related" in {node["label"] for node in both["nodes"]}


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
    assert result["edges"][0]["source"] == result["seed"]["id"]
    assert result["edges"][0]["target"] == result["target"]["id"]
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


def test_community_reaches_siblings_and_parent(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    result = _call(
        conn,
        monkeypatch,
        tmp_path,
        mode="community",
        entity="pkg.client.Client.send",
        relation_types=["member", "parent"],
    )
    labels = {node["label"] for node in result["nodes"]}
    assert "pkg.client.Client.close" in labels
    assert "pkg.client" in labels
    assert any(edge["relation"] == "member" for edge in result["edges"])
    assert any(edge["relation"] == "parent" for edge in result["edges"])


def test_impact_keeps_file_callers(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    result = _call(
        conn,
        monkeypatch,
        tmp_path,
        mode="impact",
        entity="pkg.client.Client",
    )
    assert "pkg/script.py" in {node["label"] for node in result["nodes"]}
    assert any(
        edge["relation"] == "call" and edge["target"].startswith("file:")
        for edge in result["edges"]
    )


def test_coverage_and_consistency_are_audit_evidence(
    conn, monkeypatch, tmp_path
) -> None:
    _seed(conn)
    result = _call(
        conn,
        monkeypatch,
        tmp_path,
        entity="pkg.client.Client.send",
        direction="both",
        relation_types=["mentions", "covers_pattern"],
        depth=2,
    )
    covered = [edge for edge in result["edges"] if edge["relation"] == "covers_pattern"]
    assert covered
    assert covered[0]["evidence"]["consistency"] == [
        {"facet": "coverage", "verdict": "match"}
    ]


def test_ambiguous_and_conflict_edges_are_diagnostic_by_default(
    conn, monkeypatch, tmp_path
) -> None:
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


def test_profile_scope_is_explicit(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    conn.execute(
        "INSERT INTO mining_profiles(id, version_id, name, source_ids, support_mode,"
        " params, fingerprint, tenant_id)"
        " VALUES(2, 1, 'other', '[]', 'sample', '{}', 'fp2', 'default')"
    )
    conn.execute(
        "INSERT INTO seq_patterns"
        " (id, library_id, version_id, profile_id, pattern, support, tenant_id)"
        " VALUES (3, 1, 1, 2, ?, 99, 'default')",
        (json.dumps(["pkg.client.Client", "pkg.helper.retry"]),),
    )
    conn.commit()
    default = _call(conn, monkeypatch, tmp_path, direction="forward")
    assert "pkg.helper.retry" not in {n["label"] for n in default["nodes"]}
    other = _call(conn, monkeypatch, tmp_path, direction="forward", profile="other")
    assert "pkg.helper.retry" in {n["label"] for n in other["nodes"]}
    assert other["profile"] == "other"
    with pytest.raises(ValueError, match="unsupported relation types"):
        _call(conn, monkeypatch, tmp_path, relation_types=["unknown"])


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
    assert payload["profile"] == "usage"
    rejected = client.get(
        "/graph",
        params={"entity": "pkg.client.Client", "library": "graphlib"},
    )
    assert rejected.status_code == 400
    web_conn.close()


def test_graph_query_requires_explicit_version(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    service = _service(conn, monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="library and version are required"):
        service.query(
            "neighbors", "pkg.client.Client", library="graphlib", tenant_id="default"
        )
    with pytest.raises(ValueError, match="tenant is required"):
        service.query(
            "neighbors", "pkg.client.Client", library="graphlib", version="1.0"
        )


def test_path_honors_caller_direction(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    forward = _call(
        conn,
        monkeypatch,
        tmp_path,
        mode="path",
        entity="pkg.client.Client",
        target="pkg.client.Client.send",
        direction="forward",
        relation_types=["call"],
    )
    assert forward["found"] is True
    backward = _call(
        conn,
        monkeypatch,
        tmp_path,
        mode="path",
        entity="pkg.client.Client",
        target="pkg.client.Client.send",
        direction="backward",
        relation_types=["call"],
    )
    assert backward["found"] is False


def test_usage_rejects_explicit_depth(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    with pytest.raises(ValueError, match="depth is not supported for usage"):
        _call(
            conn,
            monkeypatch,
            tmp_path,
            mode="usage",
            entity="pkg.client.Client.send",
            depth=2,
        )


def test_relation_filter_prunes_unrelated_nodes(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    result = _call(
        conn,
        monkeypatch,
        tmp_path,
        entity="pkg.client.Client.send",
        direction="backward",
        relation_types=["call"],
    )
    labels = {node["label"] for node in result["nodes"]}
    assert "pkg.client.Client" in labels
    assert "cluster:4" not in labels
    assert "API" not in labels
    assert "pkg.client.send" not in labels
    assert all(edge["relation"] == "call" for edge in result["edges"])


def test_path_reports_node_budget_truncation(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    service = GraphQueryService(
        conn,
        make_settings(monkeypatch, tmp_path, graph_max_depth=2, graph_max_nodes=2),
    )
    result = service.query(
        "path",
        "pkg.client.Client",
        target="pkg.client.Client.close",
        library="graphlib",
        version="1.0",
        tenant_id="default",
        relation_types=["call"],
    )
    assert result["found"] is False
    assert result["truncated"] is True


def test_fixed_modes_reject_inapplicable_arguments(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    with pytest.raises(ValueError, match="target is only supported"):
        _call(
            conn,
            monkeypatch,
            tmp_path,
            target="pkg.client.Client.send",
        )
    with pytest.raises(ValueError, match="community queries require"):
        _call(
            conn,
            monkeypatch,
            tmp_path,
            mode="community",
            direction="forward",
        )
    with pytest.raises(ValueError, match="unknown profile"):
        _call(conn, monkeypatch, tmp_path, profile="missing")


def test_synthetic_nodes_keep_consistent_evidence(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    result = _call(
        conn,
        monkeypatch,
        tmp_path,
        entity="pkg.client.Client.send",
        direction="backward",
    )
    cluster = next(node for node in result["nodes"] if node["kind"] == "cluster")
    row_id = int(cluster["source_id"])
    assert (
        conn.execute(
            "SELECT count(*) FROM api_clusters WHERE id = ?", (row_id,)
        ).fetchone()[0]
        == 1
    )
    member_edges = [edge for edge in result["edges"] if edge["source"] == cluster["id"]]
    assert member_edges
    for edge in member_edges:
        assert edge["source_table"] == "api_clusters"
        assert (
            conn.execute(
                "SELECT count(*) FROM api_clusters WHERE id = ?", (edge["source_id"],)
            ).fetchone()[0]
            == 1
        )
    feature = next(
        node for node in result["nodes"] if node["label"] == "pkg.client.send"
    )
    assert feature["source_id"] is None


def test_query_budget_marks_truncation(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    service = GraphQueryService(
        conn, make_settings(monkeypatch, tmp_path, graph_query_budget=2)
    )
    result = service.query(
        "neighbors",
        "pkg.client.Client",
        library="graphlib",
        version="1.0",
        tenant_id="default",
        direction="forward",
        depth=1,
    )
    assert result["edges"]
    assert result["truncated"] is True


def test_doc_link_expands_all_chunks_of_target_page(
    conn, monkeypatch, tmp_path
) -> None:
    _seed(conn)
    result = _call(
        conn,
        monkeypatch,
        tmp_path,
        entity="pkg.client.Client.send",
        direction="both",
        relation_types=["mentions", "doc_link"],
    )
    labels = {node["label"] for node in result["nodes"]}
    assert {"API", "Related", "Related Detail"} <= labels


@pytest.mark.skipif(not _PG_URL, reason="LIBREF_TEST_POSTGRES_URL not set")
def test_postgres_graph_query_reads_the_same_facts(monkeypatch, tmp_path) -> None:
    from libref.db.assembly import build_metadata_session

    _fresh_postgres(_PG_URL)
    session = build_metadata_session(_PostgresSettings())
    try:
        session.apply_migrations()
        _seed_postgres(session)
        service = GraphQueryService(session, make_settings(monkeypatch, tmp_path))
        scope = {"library": "graph-pg", "version": "1.0", "tenant_id": "default"}
        forward = service.query(
            "neighbors",
            "pkg.Client",
            direction="forward",
            depth=1,
            relation_types=["call"],
            **scope,
        )
        assert "pkg.Client.send" in {node["label"] for node in forward["nodes"]}
        assert any(edge["relation"] == "call" for edge in forward["edges"])
        backward = service.query(
            "path",
            "pkg.Client.send",
            target="pkg.Client",
            direction="backward",
            relation_types=["call"],
            **scope,
        )
        assert backward["found"] is True
        mentions = service.query(
            "neighbors",
            "pkg.Client.send",
            direction="both",
            relation_types=["mentions"],
            **scope,
        )
        assert "API" in {node["label"] for node in mentions["nodes"]}
    finally:
        session.close()


def test_gold_cases_match_seeded_source_of_truth(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    report = _service(conn, monkeypatch, tmp_path).audit(
        _GOLD["cases"],
        library="graphlib",
        version="1.0",
        tenant_id="default",
    )
    assert report["summary"]["total"] == len(_GOLD["cases"])
    assert report["summary"]["matched"] == len(_GOLD["cases"])
    assert report["summary"]["match_rate"] == 1.0
    assert report["summary"]["errors"] == 0
    assert all(case["passed"] for case in report["cases"])
    for key in (
        "seed_resolution",
        "path_hit",
        "node_precision",
        "node_recall",
        "relation_precision",
        "relation_recall",
        "evidence_traceability",
        "node_evidence_traceability",
    ):
        assert report["metrics"][key] == 1.0, key
    assert report["metrics"]["unexpected_truncations"] == 0
    assert report["metrics"]["missed_truncations"] == 0


def test_gold_cases_are_fixture_backed(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    cases = _GOLD["cases"]
    row_tables = {
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
        "documents",
    }
    names: set[str] = set()
    modes: set[str] = set()
    relations: set[str] = set()
    for case in cases:
        assert case["name"] not in names
        names.add(case["name"])
        for key in (
            "source_ref",
            "mode",
            "entity",
            "expected_seed",
            "expected_nodes",
            "expected_relations",
            "expected_evidence_refs",
        ):
            assert case.get(key) is not None, (case["name"], key)
        assert str(case["source_ref"]).startswith(
            "tests/integration/test_graph_query.py::_seed"
        )
        assert case["mode"] in GRAPH_MODES
        assert case.get("direction", "both") in GRAPH_DIRECTIONS
        assert set(case.get("relation_types") or []) <= set(GRAPH_RELATIONS)
        relations.update(case["expected_relations"])
        assert not set(case.get("forbidden_nodes") or []) & set(case["expected_nodes"])
        for ref in case["expected_evidence_refs"]:
            table, separator, marker = str(ref).partition(":")
            assert separator and table in row_tables
            row_id, separator, scope = marker.partition("@")
            assert separator and row_id.isdigit()
            version, _, tenant = scope.partition(":")
            assert version.isdigit()
            columns = {
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            sql = f"SELECT 1 FROM {table} WHERE id = ? AND version_id = ?"
            params: list[Any] = [int(row_id), int(version)]
            if "tenant_id" in columns:
                sql += " AND tenant_id = ?"
                params.append(tenant)
            assert conn.execute(sql, params).fetchone() is not None, ref
        for node_id in case["expected_nodes"]:
            kind, separator, marker = str(node_id).partition(":")
            assert separator
            if kind == "api":
                assert conn.execute(
                    "SELECT 1 FROM api_entities WHERE id = ?", (int(marker),)
                ).fetchone()
            elif kind == "doc":
                assert conn.execute(
                    "SELECT 1 FROM documents WHERE id = ?", (int(marker),)
                ).fetchone()
            elif kind == "cluster":
                assert conn.execute(
                    "SELECT 1 FROM api_clusters WHERE id = ?",
                    (int(marker.split(":")[0]),),
                ).fetchone()
            elif kind == "pattern":
                assert conn.execute(
                    "SELECT 1 FROM doc_pattern_coverage WHERE id = ?",
                    (int(marker.split(":")[0]),),
                ).fetchone()
            else:
                assert kind in {"file", "feature"}
        modes.add(case["mode"])
    assert modes == set(GRAPH_MODES)
    assert relations == set(GRAPH_RELATIONS)
    assert any(case.get("include_ambiguous") for case in cases)
    assert any(case.get("expected_found") is False for case in cases)


def test_audit_report_exposes_mismatch_without_gate(
    conn, monkeypatch, tmp_path
) -> None:
    _seed(conn)
    case = dict(_GOLD["cases"][0], expected_nodes=["api:absent"])
    report = _service(conn, monkeypatch, tmp_path).audit(
        [case],
        library="graphlib",
        version="1.0",
        tenant_id="default",
    )
    assert report["summary"]["total"] == 1
    assert report["summary"]["matched"] == 0
    assert report["cases"][0]["passed"] is False
    assert report["cases"][0]["actual_nodes"]
    assert report["cases"][0]["node_precision"] == 0.0
    assert report["metrics"]["node_recall"] == 0.0


def test_audit_checks_evidence_scope_and_expected_refs(
    conn, monkeypatch, tmp_path
) -> None:
    _seed(conn)
    service = _service(conn, monkeypatch, tmp_path)
    edge = {
        "source_table": "call_instances",
        "version_id": 1,
        "tenant_id": "default",
        "profile_id": 1,
    }
    assert service._trace_evidence("call_instances:1@1:default", edge=edge) is True
    assert service._trace_evidence("call_instances:9999@1:default", edge=edge) is False
    assert (
        service._trace_evidence(
            "call_instances:1@1:default",
            edge={**edge, "source_table": "seq_patterns"},
        )
        is False
    )
    pattern_ref = "seq_patterns:1@1:default"
    pattern_edge = {
        "source_table": "seq_patterns",
        "version_id": 1,
        "tenant_id": "default",
        "profile_id": 1,
    }
    assert service._trace_evidence(pattern_ref, edge=pattern_edge) is True
    assert (
        service._trace_evidence(pattern_ref, edge={**pattern_edge, "profile_id": 999})
        is False
    )
    case = dict(
        _GOLD["cases"][0],
        expected_evidence_refs=["call_instances:9999@1:default"],
    )
    report = service.audit(
        [case], library="graphlib", version="1.0", tenant_id="default"
    )
    assert report["cases"][0]["evidence_traceability"] == 1.0
    assert report["cases"][0]["passed"] is False
    assert report["cases"][0]["actual_evidence_refs"] == ["call_instances:1@1:default"]


def test_audit_reports_unexpected_truncation(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    service = GraphQueryService(
        conn,
        make_settings(monkeypatch, tmp_path, graph_max_nodes=2),
    )
    report = service.audit(
        [_GOLD["cases"][0]],
        library="graphlib",
        version="1.0",
        tenant_id="default",
    )
    case = report["cases"][0]
    assert case["truncated"] is True
    assert case["truncation_ok"] is False
    assert case["passed"] is False
    assert report["metrics"]["unexpected_truncations"] == 1
    assert report["metrics"]["missed_truncations"] == 0


def test_audit_separates_errors_from_truncation(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    case = dict(_GOLD["cases"][0], entity="pkg.client.Absent")
    report = _service(conn, monkeypatch, tmp_path).audit(
        [case], library="graphlib", version="1.0", tenant_id="default"
    )
    assert report["summary"]["errors"] == 1
    assert report["cases"][0]["error"] == "unknown entity: pkg.client.Absent"
    assert report["cases"][0]["truncated"] is False
    assert report["metrics"]["unexpected_truncations"] == 0
    assert report["metrics"]["missed_truncations"] == 0


def test_audit_export_tool_writes_reviewable_report(
    conn, monkeypatch, tmp_path
) -> None:
    _seed(conn)
    tool_path = Path(__file__).resolve().parents[2] / "tools" / "export_graph_audit.py"
    spec = importlib.util.spec_from_file_location("export_graph_audit", tool_path)
    assert spec is not None and spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    report = tool.build_report(
        tmp_path / "test.db",
        library="graphlib",
        version="1.0",
    )
    assert report["summary"]["matched"] == len(_GOLD["cases"])
    assert report["database"] == str(tmp_path / "test.db")
    assert report["gold"] == str(tool.DEFAULT_GOLD)
    assert all(
        case["actual_evidence_refs"] or case["expected_found"] is False
        for case in report["cases"]
    )
    text = tool.render_text_report(report)
    assert "# Graph query audit" in text
    assert "- edge evidence traceability: 1.0" in text
    assert "- node evidence traceability: 1.0" in text
    assert "- source ref: tests/integration/test_graph_query.py::_seed" in text
    assert "(expected call_instances:1@1:default)" in text


def test_audit_export_tool_supports_strict_mode(conn, monkeypatch, tmp_path) -> None:
    _seed(conn)
    tool_path = Path(__file__).resolve().parents[2] / "tools" / "export_graph_audit.py"
    spec = importlib.util.spec_from_file_location(
        "export_graph_audit_strict", tool_path
    )
    assert spec is not None and spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    mismatched = dict(_GOLD["cases"][0], expected_nodes=["api:absent"])
    gold = tmp_path / "strict-gold.json"
    gold.write_text(
        json.dumps({"cases": [mismatched]}, ensure_ascii=False), encoding="utf-8"
    )
    out = tmp_path / "strict.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "export_graph_audit.py",
            "--db",
            str(tmp_path / "test.db"),
            "--library",
            "graphlib",
            "--version",
            "1.0",
            "--gold",
            str(gold),
            "--out",
            str(out),
            "--strict",
        ],
    )
    assert tool.main() == 1
    assert out.is_file()
    text = out.with_suffix(".md").read_text(encoding="utf-8")
    assert "passed: False" in text


def test_audit_export_tool_strict_mode_rejects_empty_gold(
    conn, monkeypatch, tmp_path
) -> None:
    _seed(conn)
    tool_path = Path(__file__).resolve().parents[2] / "tools" / "export_graph_audit.py"
    spec = importlib.util.spec_from_file_location("export_graph_audit_empty", tool_path)
    assert spec is not None and spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    gold = tmp_path / "empty-gold.json"
    gold.write_text(json.dumps({"cases": []}), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "export_graph_audit.py",
            "--db",
            str(tmp_path / "test.db"),
            "--library",
            "graphlib",
            "--version",
            "1.0",
            "--gold",
            str(gold),
            "--out",
            str(tmp_path / "empty.json"),
            "--strict",
        ],
    )
    assert tool.main() == 1
