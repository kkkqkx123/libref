"""MCP serve-mcp stdio 真实握手冒烟测试（子进程 + SDK stdio client，不出网）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from tests.conftest import seed_mcp_db

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_BIN = Path(sys.executable).parent / "libref"

pytestmark = [pytest.mark.integration, pytest.mark.requires_binary]

_MARKER = "quetzalstdio"
_PAGE_URL = "https://d.example/hello.html"


def _seed_db(db_path: Path) -> None:
    seed_mcp_db(db_path, _MARKER, _PAGE_URL)


async def _handshake(db_path: Path) -> None:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=str(SERVER_BIN),
        args=["serve-mcp"],
        env={"LIBREF_DB_PATH": str(db_path)},
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert {"search_docs", "read_page", "list_libraries",
                "list_versions", "api_usage", "related_apis",
                "pattern_evidence", "search_hybrid", "answer",
                "review_pending", "review_approve", "review_conflict",
                "review_reopen", "job_show"} <= names

        res = await session.call_tool("search_docs", {"query": _MARKER})
        assert not getattr(res, "isError", False)
        assert _MARKER in res.model_dump_json().lower()

        libs = await session.call_tool("list_libraries", {})
        assert "docs" in libs.model_dump_json()


def test_serve_mcp_stdio_handshake(tmp_path):
    if not SERVER_BIN.exists():
        pytest.skip(f"server binary missing: {SERVER_BIN}")
    db_path = tmp_path / "stdio.db"
    _seed_db(db_path)
    asyncio.run(_handshake(db_path))
