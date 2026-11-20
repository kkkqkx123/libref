"""Exit-level MCP review output: every registered tool is invoked and rendered."""

from __future__ import annotations

import json

import pytest

from libref.db import apply_migrations, open_db
from tests.conftest import make_settings
from tests.e2e.render_mcp_outputs import MARKER, TOOL_ORDER, render

pytestmark = pytest.mark.integration


def test_every_mcp_tool_has_reviewable_output(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "mcp-review.db"
    conn = open_db(db_path, check_same_thread=False)
    apply_migrations(conn)
    settings = make_settings(
        monkeypatch,
        tmp_path,
        db_path=str(db_path),
        auth_mode="dev",
        discovery_offline=True,
        registry_offline=True,
        induction_enabled=True,
        simvec_dir=str(tmp_path / "simvec"),
    )
    out_dir = tmp_path / "outputs" / "mcp"
    try:
        summary = render(conn, settings, out_dir)
    finally:
        conn.close()
    assert summary["tools"] == len(TOOL_ORDER) == 27
    assert summary["unexpected_errors"] == []
    assert {item["tool"] for item in summary["skipped_network"]} == {
        "doc_probe",
        "repo_probe",
    }

    for tool in TOOL_ORDER:
        body = json.loads((out_dir / f"{tool}.txt").read_text(encoding="utf-8"))
        assert body["tool"] == tool
        assert body["cases"], f"no rendered case for {tool}"

    search = json.loads((out_dir / "search.txt").read_text(encoding="utf-8"))
    balanced = next(c for c in search["cases"] if c["case"] == "balanced")
    assert balanced["result"]["count"] > 0
    assert MARKER in json.dumps(balanced["result"], ensure_ascii=False)

    read_doc = json.loads((out_dir / "read_doc.txt").read_text(encoding="utf-8"))
    assert {c["case"] for c in read_doc["cases"]} >= {"page", "window", "section"}
    assert MARKER in (out_dir / "read_doc.txt").read_text(encoding="utf-8")

    catalog = json.loads((out_dir / "catalog.txt").read_text(encoding="utf-8"))
    assert all(c["result"]["count"] >= 1 for c in catalog["cases"])

    expected_errors = {"empty-query", "empty-url", "empty-name", "local-rejected"}
    for tool in TOOL_ORDER:
        body = json.loads((out_dir / f"{tool}.txt").read_text(encoding="utf-8"))
        for case in body["cases"]:
            if case["result"].get("_is_error"):
                assert case["case"] in expected_errors
