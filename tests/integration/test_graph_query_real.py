"""Real code-side graph gold checks over the frozen benchmark database."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

import pytest

from libref.services.factory import build_graph_query_service
from tests.conftest import make_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (Path(__file__).resolve().parents[1] / "e2e/bench/data/bench.db").exists(),
        reason="frozen benchmark database is not installed",
    ),
]

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "e2e" / "bench" / "data" / "bench.db"
GOLD_PATH = ROOT / "fixtures" / "graph_gold_real.json"
GOLD = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
ROW_TABLES = {
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


def _open() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def _version_id(conn: sqlite3.Connection, library: str, version: str) -> int:
    rows = conn.execute(
        "SELECT v.id FROM versions v JOIN libraries l ON l.id=v.library_id"
        " WHERE l.name=? AND v.version=?",
        (library, version),
    ).fetchall()
    assert len(rows) == 1, (library, version)
    return int(rows[0][0])


def _ids(marker: str) -> Iterator[int]:
    for piece in marker.split(","):
        if "-" in piece:
            start, end = piece.split("-", maxsplit=1)
            yield from range(int(start), int(end) + 1)
        else:
            yield int(piece)


def _source_rows(ref: str) -> tuple[str, str, str, list[tuple[str, int]]]:
    path, separator, locator = str(ref).partition("@")
    assert separator
    scope, separator, sections = locator.partition(":")
    assert separator
    library, separator, version = scope.partition("/")
    assert separator
    rows: list[tuple[str, int]] = []
    for section in sections.split(";"):
        table, separator, marker = section.partition("#")
        assert separator and table in ROW_TABLES
        rows.extend((table, row_id) for row_id in _ids(marker))
    return path, library, version, rows


def test_real_gold_facts_are_fixture_backed() -> None:
    conn = _open()
    try:
        names: set[str] = set()
        scopes: set[tuple[str, str]] = set()
        for case in GOLD["cases"]:
            assert case["name"] not in names
            names.add(case["name"])
            scope = case["scope"]
            path, library, version, rows = _source_rows(case["source_ref"])
            assert path == "tests/e2e/bench/data/bench.db"
            assert (scope["library"], scope["version"]) == (library, version)
            scopes.add((library, version))
            version_id = _version_id(conn, library, version)
            for table, row_id in rows:
                row = conn.execute(
                    f"SELECT version_id FROM {table} WHERE id = ?", (row_id,)
                ).fetchone()
                assert row is not None, (case["name"], table, row_id)
                assert int(row[0]) == version_id
            for ref in case["expected_evidence_refs"]:
                table, separator, marker = str(ref).partition(":")
                assert separator and table in ROW_TABLES
                row_id, separator, tail = marker.partition("@")
                assert separator and row_id.isdigit()
                ref_version, _, tenant = tail.partition(":")
                columns = {
                    str(column[1])
                    for column in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                sql = f"SELECT version_id FROM {table} WHERE id = ?"
                params: list[Any] = [int(row_id)]
                if "tenant_id" in columns:
                    sql += " AND tenant_id = ?"
                    params.append(tenant)
                row = conn.execute(sql, params).fetchone()
                assert row is not None, ref
                assert int(row[0]) == int(ref_version)
            for node_id in case["expected_nodes"]:
                kind, separator, marker = str(node_id).partition(":")
                if not separator or kind != "api":
                    continue
                assert conn.execute(
                    "SELECT 1 FROM api_entities WHERE id = ?", (int(marker),)
                ).fetchone()
        assert scopes == {
            ("gradle", "v9.9.1"),
            ("httpx", "7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016"),
        }
        assert {case["mode"] for case in GOLD["cases"]} == {
            "neighbors",
            "path",
            "impact",
            "usage",
        }
        relations = {
            relation
            for case in GOLD["cases"]
            for relation in case["expected_relations"]
        }
        assert relations == {"call", "import"}
    finally:
        conn.close()


def test_real_gold_audit_matches_frozen_database(monkeypatch, tmp_path) -> None:
    conn = _open()
    tool_path = ROOT.parent / "tools" / "export_graph_audit.py"
    spec = importlib.util.spec_from_file_location("real_graph_audit", tool_path)
    assert spec is not None and spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    try:
        settings = make_settings(monkeypatch, tmp_path, db_path=str(DB))
        report = build_graph_query_service(conn, settings).audit(GOLD["cases"])
        assert report["summary"] == {
            "total": len(GOLD["cases"]),
            "matched": len(GOLD["cases"]),
            "match_rate": 1.0,
            "errors": 0,
        }
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
        assert report["cases"][0]["scope"]["library"] == "gradle"
        assert report["cases"][-1]["scope"]["library"] == "httpx"

        exported = tool.build_report(DB, gold_path=GOLD_PATH)
        assert exported["summary"] == report["summary"]
        assert exported["metrics"] == report["metrics"]
        text = tool.render_text_report(exported)
        assert "- library: -" in text
        assert "- scope: library=gradle, version=v9.9.1" in text
        assert "- scope: library=httpx," in text
    finally:
        conn.close()
