"""MCP serve-mcp stdio real handshake smoke test (subprocess + SDK stdio client, no network)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from tests.conftest import seed_mcp_db

SERVER_BIN = Path(sys.executable).parent / "libref"

pytestmark = [pytest.mark.integration, pytest.mark.requires_binary]

_MARKER = "quetzalstdio"
_PAGE_URL = "https://d.example/hello.html"


def _seed_db(db_path: Path) -> None:
    seed_mcp_db(db_path, _MARKER, _PAGE_URL)


async def _handshake(db_path: Path, workdir: Path) -> None:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=str(SERVER_BIN),
        args=["serve-mcp"],
        env={"LIBREF_DB_PATH": str(db_path)},
        cwd=str(workdir),
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert {
            "search",
            "read_doc",
            "catalog",
            "api_insight",
            "pattern_insight",
            "answer",
            "review",
            "job_show",
            "resolve",
        } <= names

        res = await session.call_tool(
            "search", {"query": _MARKER, "preset": "balanced"}
        )
        assert not getattr(res, "isError", False)
        assert _MARKER in res.model_dump_json().lower()

        libs = await session.call_tool("catalog", {})
        assert "docs" in libs.model_dump_json()


def test_serve_mcp_stdio_handshake(tmp_path, monkeypatch):
    if not SERVER_BIN.exists():
        pytest.skip(f"server binary missing: {SERVER_BIN}")
    db_path = tmp_path / "stdio.db"
    _seed_db(db_path)
    asyncio.run(_handshake(db_path, tmp_path))
