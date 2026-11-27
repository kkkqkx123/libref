"""Graph query integration tests over seeded source-of-truth relations."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from libref.services.graph_query import GraphQueryService
from tests.conftest import make_settings, seed_library_version

pytestmark = pytest.mark.integration

_GOLD = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "graph_gold.json").read_text(
        encoding="utf-8"
    )
)


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
            (4, "d", "pkg.client.Client.close", "method"),
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
            (
                3,
                "https://g.example/related",
                "related body",
                json.dumps({"title": "Related"}),
                2,
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
    assert "Related" in labels
    assert "Weak" not in labels
    assert "cluster:4" in labels
    assert "pkg.client.Client" in labels
    assert "pkg/entry.py" in labels
    assert backward["profile"] == "usage"
    assert backward["tenant_id"] == "default"


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
        direction="backward",
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
    assert all(case["passed"] for case in report["cases"])


def test_audit_report_exposes_mismatch_without_gate(
    conn, monkeypatch, tmp_path
) -> None:
    _seed(conn)
    case = dict(_GOLD["cases"][0], expected_nodes=["pkg.absent.Endpoint"])
    report = _service(conn, monkeypatch, tmp_path).audit(
        [case],
        library="graphlib",
        version="1.0",
        tenant_id="default",
    )
    assert report["summary"] == {"total": 1, "matched": 0}
    assert report["cases"][0]["passed"] is False
    assert report["cases"][0]["actual_nodes"]


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
    assert all(case["evidence_refs"] for case in report["cases"])
