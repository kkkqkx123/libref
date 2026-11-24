"""MCP and Web expose the same retrieval status contract."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from libref.db import apply_migrations, open_db
from libref.mcp_server import build_mcp_server
from libref.web import build_web_app
from tests.conftest import make_settings, seed_mcp_db

pytestmark = pytest.mark.integration

_MARKER = "quetzalmcp"
_PAGE_URL = "https://docs.example/contract.html"
_STATUS_FIELDS = (
    "degraded",
    "reasons",
    "answerable",
    "answerability",
    "cross_lingual",
)


def test_search_status_fields_match_between_mcp_and_web(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "contract.db"
    settings = make_settings(
        monkeypatch,
        tmp_path,
        db_path=str(db_path),
        auth_mode="dev",
        discovery_offline=True,
        registry_offline=True,
    )
    seed_mcp_db(db_path, _MARKER, _PAGE_URL)
    conn = open_db(db_path, check_same_thread=False)
    apply_migrations(conn)
    web = TestClient(build_web_app(conn, settings))
    mcp = build_mcp_server(conn, settings)
    try:
        http_payload = web.get("/search", params={"query": _MARKER}).json()
        result = asyncio.run(
            mcp.call_tool("search", {"query": _MARKER, "preset": "balanced"})
        )
        tool_payload = dict(result.structured_content or {})
        assert http_payload["count"] == tool_payload["count"] > 0
        for field in _STATUS_FIELDS:
            assert field in http_payload, field
            assert field in tool_payload, field
            assert http_payload[field] == tool_payload[field], field
    finally:
        web.close()
        conn.close()
