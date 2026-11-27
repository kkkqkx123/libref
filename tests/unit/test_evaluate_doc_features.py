import sqlite3
from pathlib import Path

from tests.e2e.bench import evaluate_doc_features


class FakeInsight:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def links(
        self, version_id: int, *, document_id: int, limit: int
    ) -> list[dict[str, object]]:
        if document_id != 1:
            return []
        return [
            {
                "document_id": 1,
                "api": "demo.Client.connect",
                "match_kind": "exact",
                "confidence": 0.9,
                "evidence": "{}",
                "nodes": [],
            }
        ]

    def features(
        self,
        version_id: int,
        *,
        document_id: int,
        limit: int,
    ) -> list[dict[str, object]]:
        if document_id != 1:
            return []
        return [
            {
                "document_id": 1,
                "facet": "block_type",
                "details": {"value": "example"},
            },
            {
                "document_id": 1,
                "facet": "heading_depth",
                "details": {"value": 2},
            },
        ]

    def consistency(
        self,
        version_id: int,
        library_id: int,
        *,
        document_id: int,
        limit: int,
    ) -> list[dict[str, object]]:
        if document_id != 1:
            return []
        return [{"document_id": 1, "verdict": "aligned"}]


def test_score_entry_matches_expected_feature_node_subset(monkeypatch: object) -> None:
    from libref.services.docs import doc_insight
    from libref.services.registry import libraries

    monkeypatch.setattr(doc_insight, "DocInsightService", FakeInsight)
    monkeypatch.setattr(
        libraries.LibraryService, "entity_source_version_id", lambda self, vid: 2
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE documents (id INTEGER, url TEXT, version_id INTEGER, metadata TEXT)"
    )
    conn.execute("CREATE TABLE versions (id INTEGER, library_id INTEGER)")
    conn.execute(
        "CREATE TABLE api_entities (id INTEGER, qualified_name TEXT, version_id INTEGER)"
    )
    conn.execute(
        "CREATE TABLE doc_feature_node_links ("
        "version_id INTEGER, document_id INTEGER, node_path TEXT)"
    )
    conn.execute(
        "CREATE TABLE doc_api_links ("
        "version_id INTEGER, document_id INTEGER, api_entity_id INTEGER,"
        " match_kind TEXT)"
    )
    conn.execute(
        "INSERT INTO documents VALUES (1, 'https://example.test/api', 1,"
        ' \'{"path": "Client / Connect"}\')'
    )
    conn.execute("INSERT INTO versions VALUES (1, 7)")
    conn.execute("INSERT INTO api_entities VALUES (1, 'demo.Client.connect', 2)")
    conn.execute("INSERT INTO doc_api_links VALUES (1, 1, 1, 'symbol')")
    conn.executemany(
        "INSERT INTO doc_feature_node_links VALUES (?, ?, ?)",
        [(1, 1, "demo"), (1, 1, "demo.Client"), (1, 1, "demo.Client.connect")],
    )

    question = {
        "id": "N1",
        "targets": ["https://example.test/api"],
        "expected_entities": ["demo.Client.connect"],
        "expected_facets": {
            "heading": "Connect",
            "block_type": "example",
            "heading_depth": 2,
        },
        "expected_feature_nodes": ["demo.Client", "demo.Client.connect"],
        "expected_verdict": "aligned",
    }
    row = evaluate_doc_features._score_entry(conn, question, 1)
    assert row["anchor_problems"] == []
    assert row["facet_match"] is True
    assert row["entity_match_strong"] is True
    assert row["node_match"] is True
    assert row["nodes_found"] == ["demo", "demo.Client", "demo.Client.connect"]

    question["expected_feature_nodes"] = ["demo.Missing"]
    row = evaluate_doc_features._score_entry(conn, question, 1)
    assert row["node_match"] is False
    assert row["nodes_expected"] == ["demo.Missing"]

    wrong_entity = dict(question, expected_entities=["demo.Missing"])
    row = evaluate_doc_features._score_entry(conn, wrong_entity, 1)
    assert row["entity_match"] is False

    wrong_verdict = dict(question, expected_verdict="drift")
    row = evaluate_doc_features._score_entry(conn, wrong_verdict, 1)
    assert row["verdict_match"] is False

    unlabeled = dict(question)
    unlabeled["expected_entities"] = []
    unlabeled["expected_feature_nodes"] = []
    unlabeled["expected_verdict"] = ""
    row = evaluate_doc_features._score_entry(conn, unlabeled, 1)
    assert row["entity_match"] is None
    assert row["node_match"] is None
    assert row["verdict_match"] is None


def test_match_stats_ignore_unlabeled_rows() -> None:
    entries = [
        {"entity_match": True},
        {"entity_match": None},
        {"entity_match": False},
    ]
    assert evaluate_doc_features._match_stats(entries, "entity_match") == (1, 2)


def test_report_marks_empty_labels_unmeasurable(tmp_path: Path) -> None:
    entry = {
        "id": "N1",
        "anchor_problems": [],
        "entities_expected": [],
        "entities_found": [],
        "entity_match": None,
        "entity_match_strong": None,
        "nodes_expected": [],
        "nodes_found": [],
        "node_match": None,
        "facets_expected": {"block_type": "example"},
        "facet_checks": {"block_type": True},
        "facet_match": True,
        "block_type_expected": "example",
        "block_type_match": True,
        "verdict_expected": "",
        "verdict_match": None,
    }
    second = dict(
        entry,
        id="N2",
        entities_expected=["demo.Client"],
        entity_match=True,
        entity_match_strong=True,
    )
    result = {
        "name": "demo/docs",
        "revision": "r1",
        "source": "https://example.test",
        "entity_source": "demo/code",
        "snippet_counts": {"snippets": 0},
        "derivation": {"coverage": 0},
        "questions": [entry, second],
        "ranks": {"N1": 1, "N2": 1},
        "filters": {},
        "annotations": {
            "top_n": 5,
            "slots": 1,
            "annotated": 1,
            "outside_top_n": 0,
            "changed_snippets": 1,
        },
        "reviews": [
            {
                "id": "N1",
                "query": "demo query",
                "items": [
                    {
                        "rank": 1,
                        "id": 1,
                        "url": "https://example.test",
                        "path": "Demo",
                        "title": "Demo",
                        "score": 1.0,
                        "raw": "raw body",
                        "annotated": "raw body\n\n[derived] block: example",
                        "annotations": ["block: example"],
                    }
                ],
            }
        ],
    }
    path = evaluate_doc_features._report(
        [result],
        {"demo/docs": [{"id": "N1", "source": "docs"}]},
        5,
        tmp_path,
    )
    text = path.read_text(encoding="utf-8")
    assert (
        "| N1 | docs | OK | unmeasurable | unmeasurable | unmeasurable | OK | OK | unmeasurable | 1 |"
        in text
    )
    review = evaluate_doc_features._write_annotation_review([result], tmp_path, 5)
    body = (review / "demo_docs" / "N1.md").read_text(encoding="utf-8")
    assert "### Raw snippet" in body
    assert "### Annotated snippet" in body
    assert "- block: example" in body
    assert "- 实体对齐：1/1（有标签题）" in text
    assert "- 强边对齐：1/1（有标签题）" in text
    assert "- 树节点一致：0/0（有标签题，不可测量）" in text
    assert "- 判定一致：0/0（有标签题，不可测量）" in text


def test_filter_stats_use_eligible_questions_and_heading_hits() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE doc_features (document_id INTEGER, facet TEXT, value TEXT)"
    )
    conn.execute(
        "CREATE TABLE doc_api_links ("
        "document_id INTEGER, api_entity_id INTEGER, match_kind TEXT)"
    )
    conn.executemany(
        "INSERT INTO doc_features VALUES (?, ?, ?)",
        [
            (1, "block_type", '{"kind": "example"}'),
            (2, "block_type", '{"kind": "narrative"}'),
            (2, "deprecated", '{"details": {"value": 1}}'),
        ],
    )
    conn.execute("INSERT INTO doc_api_links VALUES (1, 1, 'symbol')")
    question = {
        "targets": ["https://example.test/api"],
        "expected_facets": {"heading": "Connect", "block_type": "example"},
    }
    found = [
        {
            "id": 1,
            "url": "https://example.test/api",
            "path": "Client / Connect",
        },
        {"id": 2, "url": "https://example.test/api", "path": "Client / Notes"},
    ]
    eligible = {
        "examples_only": True,
        "exclude_deprecated": True,
        "aligned_only": True,
    }
    row = evaluate_doc_features._filter_stats(conn, found, question, eligible)
    assert row["examples_only"]["kept"] == 1
    assert row["examples_only"]["eligible_hits"] == 1
    assert row["exclude_deprecated"]["kept"] == 1
    assert row["aligned_only"]["kept"] == 1


def test_eligibility_uses_strong_links_only() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE doc_api_links ("
        "version_id INTEGER, document_id INTEGER, match_kind TEXT)"
    )
    conn.executemany(
        "INSERT INTO doc_api_links VALUES (?, ?, ?)",
        [(1, 1, "name_fuzzy"), (1, 2, "symbol")],
    )
    question = {
        "expected_facets": {"block_type": "example", "deprecated": True},
    }
    assert evaluate_doc_features._eligibility(conn, question, 1, {1}) == {
        "examples_only": True,
        "exclude_deprecated": False,
        "aligned_only": False,
    }
    assert evaluate_doc_features._eligibility(conn, question, 1, {2})["aligned_only"]
