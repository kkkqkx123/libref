"""Shared test fixtures: temporary DB connection (migrations applied) and reusable DB-seeding helpers."""

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
        elif "/tests/e2e/" in path:
            item.add_marker(pytest.mark.e2e)


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = open_db(tmp_path / "test.db")
    apply_migrations(c)
    yield c
    c.close()


def seed_library_version(
    conn: sqlite3.Connection, library: str = "docs", version: str = "1.0"
) -> tuple[int, int]:
    """Library and version FK rows for tests, fixed to ids 1."""
    conn.execute("INSERT INTO libraries(id, name) VALUES(1, ?)", (library,))
    conn.execute("INSERT INTO versions(id, library_id, version) VALUES(1, 1, ?)", (version,))
    conn.commit()
    return 1, 1


def seed_mcp_db(db_path: Path, marker: str, page_url: str) -> None:
    """Minimal DB for MCP handshake smoke tests: one version, one page, content carrying the marker."""
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
    """Copy tests/fixtures/mini_repo to a temp dir; path resolved relative to this file, no cwd dependency."""
    src = Path(__file__).resolve().parent / "fixtures" / "mini_repo"
    dst = tmp_path / "mini_repo"
    shutil.copytree(src, dst)
    return dst


@contextlib.contextmanager
def mock_ingest(handler) -> Iterator[httpx.AsyncClient]:
    """Swap ingest._client for a MockTransport client; no network; restored and closed on exit."""
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
