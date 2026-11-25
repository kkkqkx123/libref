import sqlite3

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
    conn.execute("CREATE TABLE api_entities (qualified_name TEXT, version_id INTEGER)")
    conn.execute(
        "CREATE TABLE doc_feature_node_links ("
        "version_id INTEGER, document_id INTEGER, node_path TEXT)"
    )
    conn.execute(
        "INSERT INTO documents VALUES (1, 'https://example.test/api', 1,"
        ' \'{"path": "Client / Connect"}\')'
    )
    conn.execute("INSERT INTO versions VALUES (1, 7)")
    conn.execute("INSERT INTO api_entities VALUES ('demo.Client.connect', 2)")
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
    assert row["node_match"] is True
    assert row["nodes_found"] == ["demo", "demo.Client", "demo.Client.connect"]

    question["expected_feature_nodes"] = ["demo.Missing"]
    row = evaluate_doc_features._score_entry(conn, question, 1)
    assert row["node_match"] is False
    assert row["nodes_expected"] == ["demo.Missing"]
