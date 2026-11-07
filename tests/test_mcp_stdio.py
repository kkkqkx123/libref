"""MCP serve-mcp stdio 真实握手冒烟测试（子进程 + SDK stdio client，不出网）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_BIN = Path(sys.executable).parent / "libref"

_MARKER = "quetzalstdio"
_PAGE_URL = "https://d.example/hello.html"


def _seed_db(db_path: Path) -> None:
    from libref.chunking import Chunk
    from libref.db import apply_migrations, open_db
    from libref.store.document_store import DocumentStore

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        apply_migrations(conn)
        conn.execute("INSERT INTO libraries(name) VALUES('docs')")
        conn.execute("INSERT INTO versions(library_id, version) VALUES(1, '1.0')")
        conn.commit()
        DocumentStore(conn).store_page(
            1, 1, _PAGE_URL, "stored",
            chunks=[Chunk(url=_PAGE_URL, path="Guide", title="Guide",
                          content=f"hello libref {_MARKER} marker", sort_order=0)],
            raw_content="seed body",
        )
    finally:
        conn.close()


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
                "list_versions"} <= names

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
