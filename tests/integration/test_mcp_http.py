"""MCP serve-mcp Streamable HTTP handshake smoke test (subprocess + SDK HTTP client, no network)."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import seed_mcp_db

SERVER_BIN = Path(sys.executable).parent / "libref"

pytestmark = [pytest.mark.integration, pytest.mark.requires_binary]

_MARKER = "quetzalhttp"
_PAGE_URL = "https://d.example/hello.html"


def _seed_db(db_path: Path) -> None:
    seed_mcp_db(db_path, _MARKER, _PAGE_URL)


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
            async with (
                streamable_http_client(url) as (read, write),
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
                } <= names

                res = await session.call_tool(
                    "search", {"query": _MARKER, "mode": "hybrid"}
                )
                assert not getattr(res, "isError", False)
                assert _MARKER in res.model_dump_json().lower()
                return
        except Exception as exc:  # noqa: BLE001 — retry while the service is starting
            last = exc
            await asyncio.sleep(0.2)
    raise AssertionError(f"http handshake failed: {last}")


def test_serve_mcp_http_handshake(tmp_path, monkeypatch):
    if not SERVER_BIN.exists():
        pytest.skip(f"server binary missing: {SERVER_BIN}")
    db_path = tmp_path / "http.db"
    _seed_db(db_path)
    port = _free_port()
    env = {**os.environ, "LIBREF_DB_PATH": str(db_path)}
    proc = subprocess.Popen(
        [str(SERVER_BIN), "serve-mcp", "--http", "--port", str(port)],
        env=env,
        cwd=str(tmp_path),
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
