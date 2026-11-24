"""Golden and differential checks for document-to-code alignment."""

from __future__ import annotations

import json
from pathlib import Path

from libref.mining.doc_alignment import (
    align_documents,
    build_api_index,
    compare_consistency,
    cover_patterns,
    link_feature_nodes,
    pattern_hash,
)

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "doc_features" / "golden.json"


def _golden():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_golden_ast_links_win_over_fuzzy_matches():
    data = _golden()
    index = build_api_index(data["entities"])
    links = align_documents(
        data["blocks"], index, data["snippet_calls"], min_confidence=0.4
    )
    assert [
        (row["document_id"], row["api_entity_id"], row["match_kind"])
        for row in links
        if row["document_id"] == 1
    ] == [
        (row["document_id"], row["api_entity_id"], row["match_kind"])
        for row in data["expected_links"]
    ]
    assert all(row["confidence"] >= 0.4 for row in links)


def test_ambiguous_short_name_is_dropped():
    index = build_api_index(
        [
            {"id": 1, "qualified_name": "a.Client.connect"},
            {"id": 2, "qualified_name": "b.Client.connect"},
        ]
    )
    links = align_documents(
        [{"document_id": 1, "content": "connect()", "code_hashes": [], "url": "u"}],
        index,
        [],
        min_confidence=0.4,
    )
    assert links == []


def test_feature_node_links_follow_golden_parent_chains():
    data = _golden()
    names = {row["id"]: row["qualified_name"] for row in data["entities"]}
    links = [
        {"document_id": row["document_id"], "api_entity_id": row["api_entity_id"]}
        for row in data["expected_links"]
    ]
    rows = link_feature_nodes(links, names, data["feature_nodes"])
    actual = {
        (row["document_id"], row["api_entity_id"], row["node_path"]) for row in rows
    }
    expected = {
        (row["document_id"], row["api_entity_id"], row["node_path"])
        for row in data["expected_feature_nodes"]
    }
    assert actual == expected


def test_feature_node_links_skip_names_absent_from_tree():
    rows = link_feature_nodes(
        [{"document_id": 1, "api_entity_id": 8}],
        {8: "demo.Client.missing"},
        [{"path": "demo", "parent": ""}],
    )
    assert rows == []


def test_sequence_coverage_and_drift():
    data = _golden()
    patterns = [
        {**row, "pattern_hash": pattern_hash(row["pattern"])}
        for row in data["patterns"]
    ]
    coverage = cover_patterns(data["blocks"], patterns, min_support=2)
    assert any(row["covered_kind"] == "example_code" for row in coverage)
    links = [
        {"document_id": row["document_id"], "api_entity_id": row["api_entity_id"]}
        for row in data["expected_links"]
    ]
    verdicts = compare_consistency(data["blocks"], links, coverage, min_support=2)
    by_doc = {row["document_id"]: row for row in verdicts}
    assert by_doc[1]["verdict"] == "match"
    assert by_doc[2]["verdict"] == "drift"
    assert by_doc[2]["facet"] == "deprecated_usage"
