"""MCP closed-loop tools: scope binding, denial, and structured tool output."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator

import pytest

from libref.db import apply_migrations, open_db
from libref.mcp_server import build_mcp_server
from libref.services.infra.auth import Principal, current_principal
from tests.conftest import make_settings

pytestmark = pytest.mark.unit


@pytest.fixture()
def mcp(monkeypatch, tmp_path) -> Iterator:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in [name for name in os.environ if name.startswith("LIBREF_")]:
        monkeypatch.delenv(key, raising=False)
    db_path = tmp_path / "mcp-requests.db"
    conn = open_db(db_path, check_same_thread=False)
    apply_migrations(conn)
    settings = make_settings(monkeypatch, tmp_path, db_path=str(db_path))
    try:
        yield build_mcp_server(conn, settings)
    finally:
        current_principal.set(None)
        conn.close()


def _names(server) -> set[str]:
    return {tool.name for tool in asyncio.run(server.list_tools())}


def _call(server, name: str, args: dict):
    result = asyncio.run(server.call_tool(name, args))
    return result.structured_content


def _failure(server, name: str, args: dict) -> dict:
    """Failure payload of a rejected call: the runner raises, the exit renders."""
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as caught:
        asyncio.run(server.call_tool(name, args))
    return json.loads(str(caught.value).partition(": ")[2])


def test_index_request_tools_are_visible(mcp) -> None:
    expected = {
        "request_repo_index",
        "request_docs_index",
        "index_request_status",
        "wait_index_request",
        "list_index_requests",
        "review_index_request",
    }
    assert expected <= _names(mcp)


def test_unauthorized_request_and_approval_are_denied(mcp) -> None:
    token = current_principal.set(None)
    try:
        result = _failure(
            mcp,
            "request_repo_index",
            {"library": "demo", "version": "1.0", "source": "https://github.com/a/b"},
        )
        assert result["error"] == "unauthorized"
        approval = _failure(mcp, "review_index_request", {"action": "list"})
        assert approval["error"] == "unauthorized"
    finally:
        current_principal.reset(token)


def test_low_privilege_request_status_and_wait_loop(mcp) -> None:
    principal = Principal(
        id=11,
        tenant_id="default",
        name="dev",
        roles=["external"],
        scopes={"read", "index:request"},
    )
    token = current_principal.set(principal)
    try:
        created = _call(
            mcp,
            "request_repo_index",
            {
                "library": "demo",
                "version": "1.0",
                "source": "https://github.com/org/repo.git",
            },
        )
        assert created["status"] == "pending"
        assert "wait_index_request" in created["next_tools"]
        status = _call(mcp, "index_request_status", {"request_id": created["id"]})
        assert status["id"] == created["id"]
        waited = _call(
            mcp,
            "wait_index_request",
            {"request_id": created["id"], "timeout": 0, "poll": 0.01},
        )
        assert waited["timed_out"] is True
        listed = _call(mcp, "list_index_requests", {})
        assert [row["id"] for row in listed["items"]] == [created["id"]]
    finally:
        current_principal.reset(token)


def test_approver_can_list_queue_and_reject(mcp) -> None:
    requester = Principal(
        id=12,
        tenant_id="default",
        name="dev2",
        roles=["external"],
        scopes={"read", "index:request"},
    )
    token = current_principal.set(requester)
    try:
        created = _call(
            mcp,
            "request_docs_index",
            {
                "library": "demo",
                "version": "1.0",
                "start_url": "https://docs.example.com/a",
            },
        )
    finally:
        current_principal.reset(token)

    approver = Principal(
        id=13,
        tenant_id="default",
        name="kb",
        roles=["kb_admin"],
        scopes={"read", "index:approve"},
    )
    token = current_principal.set(approver)
    try:
        queue = _call(mcp, "review_index_request", {"action": "list"})
        assert queue["count"] == 1
        rejected = _call(
            mcp,
            "review_index_request",
            {"action": "reject", "request_id": created["id"], "reason": "duplicate"},
        )
        assert rejected["status"] == "rejected"
    finally:
        current_principal.reset(token)
