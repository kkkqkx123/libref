"""MCP serve-mcp Streamable HTTP 握手冒烟测试（子进程 + SDK HTTP client，不出网）。"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_BIN = Path(sys.executable).parent / "libref"

_MARKER = "quetzalhttp"
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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _handshake(port: int) -> None:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = f"http://127.0.0.1:{port}/mcp"
    last: Exception | None = None
    for _ in range(100):
        try:
            async with streamable_http_client(url) as (read, write), \
                    ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert {"search_docs", "read_page", "list_libraries",
                        "list_versions", "api_usage", "related_apis",
                        "pattern_evidence"} <= names

                res = await session.call_tool("search_docs", {"query": _MARKER})
                assert not getattr(res, "isError", False)
                assert _MARKER in res.model_dump_json().lower()
                return
        except Exception as exc:  # noqa: BLE001 —— 服务启动中则重试
            last = exc
            await asyncio.sleep(0.2)
    raise AssertionError(f"http handshake failed: {last}")


def test_serve_mcp_http_handshake(tmp_path):
    if not SERVER_BIN.exists():
        pytest.skip(f"server binary missing: {SERVER_BIN}")
    db_path = tmp_path / "http.db"
    _seed_db(db_path)
    port = _free_port()
    env = {**os.environ, "LIBREF_DB_PATH": str(db_path)}
    proc = subprocess.Popen(
        [str(SERVER_BIN), "serve-mcp", "--http", "--port", str(port)],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        asyncio.run(_handshake(port))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
