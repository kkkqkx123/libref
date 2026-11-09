"""测试公共 fixture：临时库连接（已应用迁移）与跨文件复用的建库助手。"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sqlite3
from pathlib import Path
from typing import Iterator

import httpx
import pytest

from libref.db import apply_migrations, open_db


def pytest_collection_modifyitems(items) -> None:
    for item in items:
        path = str(getattr(item, "fspath", ""))
        if "/tests/unit/" in path:
            item.add_marker(pytest.mark.unit)
        elif "/tests/integration/" in path:
            item.add_marker(pytest.mark.integration)


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = open_db(tmp_path / "test.db")
    apply_migrations(c)
    yield c
    c.close()


def seed_library_version(
    conn: sqlite3.Connection, library: str = "docs", version: str = "1.0"
) -> tuple[int, int]:
    """测试库与版本外键行，固定标识为 1。"""
    conn.execute("INSERT INTO libraries(id, name) VALUES(1, ?)", (library,))
    conn.execute("INSERT INTO versions(id, library_id, version) VALUES(1, 1, ?)", (version,))
    conn.commit()
    return 1, 1


def seed_mcp_db(db_path: Path, marker: str, page_url: str) -> None:
    """MCP 握手冒烟用的最小库：单版本单页面，内容携带 marker。"""
    from libref.chunking import Chunk
    from libref.store.document_store import DocumentStore

    db_path.parent.mkdir(parents=True, exist_ok=True)
    c = open_db(db_path)
    try:
        apply_migrations(c)
        c.execute("INSERT INTO libraries(name) VALUES('docs')")
        c.execute("INSERT INTO versions(library_id, version) VALUES(1, '1.0')")
        c.commit()
        DocumentStore(c).store_page(
            1,
            1,
            page_url,
            "stored",
            chunks=[
                Chunk(
                    url=page_url,
                    path="Guide",
                    title="Guide",
                    content=f"hello libref {marker} marker",
                    sort_order=0,
                )
            ],
            raw_content="seed body",
        )
    finally:
        c.close()


def copy_mini_repo(tmp_path: Path) -> Path:
    """复制 tests/fixtures/mini_repo 到临时目录，路径相对本文件解析，不依赖 cwd。"""
    src = Path(__file__).resolve().parent / "fixtures" / "mini_repo"
    dst = tmp_path / "mini_repo"
    shutil.copytree(src, dst)
    return dst


@contextlib.contextmanager
def mock_ingest(handler) -> Iterator[httpx.AsyncClient]:
    """将 ingest._client 替换为 MockTransport 客户端，不出网，退出时复原并关闭。"""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    @contextlib.asynccontextmanager
    async def fake(_settings):
        yield client

    import libref.services.ingest as mod

    original = mod._client
    mod._client = fake
    try:
        yield client
    finally:
        mod._client = original
        asyncio.run(client.aclose())
