"""Shared test fixtures: temporary DB connection (migrations applied) and reusable DB-seeding helpers."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from libref.config import LibrefSettings
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


def clear_libref_env(monkeypatch, tmp_path) -> None:
    """Hermetic shell for settings construction: clean cwd plus no LIBREF_* leak."""
    monkeypatch.chdir(tmp_path)
    for name in [n for n in os.environ if n.startswith("LIBREF_")]:
        monkeypatch.delenv(name, raising=False)


def make_settings(monkeypatch, tmp_path, **overrides) -> LibrefSettings:
    """Canonical test settings: hermetic construction plus explicit overrides only."""
    clear_libref_env(monkeypatch, tmp_path)
    return LibrefSettings(**overrides)


def make_crawler_settings(monkeypatch, tmp_path, **overrides) -> LibrefSettings:
    """Fast local-crawl bundle: no politeness delays, short timeouts; MockTransport supplies the bytes."""
    base = {
        "request_timeout": 5.0,
        "fetch_concurrency": 2,
        "crawl_delay_default": 0.0,
        "robots_cache_ttl_days": 1.0,
    }
    base.update(overrides)
    return make_settings(monkeypatch, tmp_path, **base)


def make_registry_settings(monkeypatch, tmp_path, **overrides) -> LibrefSettings:
    """Registry resolution bundle: short cache TTL, explicit offline and privacy toggles."""
    base = {
        "request_timeout": 5.0,
        "registry_cache_ttl_days": 1.0,
        "registry_offline": False,
        "registry_allow_private": False,
    }
    base.update(overrides)
    return make_settings(monkeypatch, tmp_path, **base)


def make_discovery_settings(monkeypatch, tmp_path, **overrides) -> LibrefSettings:
    """Library discovery bundle: short cache TTL, explicit offline and enable toggles."""
    base = {
        "request_timeout": 5.0,
        "discovery_cache_ttl_days": 1.0,
        "discovery_offline": False,
        "discovery_enabled": True,
    }
    base.update(overrides)
    return make_settings(monkeypatch, tmp_path, **base)


@pytest.fixture(autouse=True)
def _isolated_libref_env(request, tmp_path, monkeypatch):
    """Hermetic LIBREF_* environment for integration tests: the repo-root .env must not leak in."""
    if "/tests/integration/" not in str(getattr(request.node, "fspath", "")):
        yield
        return
    clear_libref_env(monkeypatch, tmp_path)
    yield


@pytest.fixture(autouse=True)
def _clear_seed_cache():
    """Isolate process-local discovery caches between tests sharing mock hosts."""
    from libref.crawler.url_seeder import _SEED_CACHE
    from libref.services.code.probe import _PROBE_CACHE
    from libref.services.docs.probe import _DOC_PROBE_CACHE

    _SEED_CACHE.clear()
    _PROBE_CACHE.clear()
    _DOC_PROBE_CACHE.clear()
    yield
    _SEED_CACHE.clear()
    _PROBE_CACHE.clear()
    _DOC_PROBE_CACHE.clear()


def seed_library_version(
    conn: sqlite3.Connection, library: str = "docs", version: str = "1.0"
) -> tuple[int, int]:
    """Library and version FK rows for tests, fixed to ids 1."""
    conn.execute("INSERT INTO libraries(id, name) VALUES(1, ?)", (library,))
    conn.execute(
        "INSERT INTO versions(id, library_id, version) VALUES(1, 1, ?)", (version,)
    )
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


def copy_tri_repo(tmp_path: Path) -> Path:
    """Copy tests/fixtures/mini_repo_tri to a temp dir."""
    src = Path(__file__).resolve().parent / "fixtures" / "mini_repo_tri"
    dst = tmp_path / "mini_repo_tri"
    shutil.copytree(src, dst)
    return dst


def write_symbol_repo(root: Path) -> None:
    """Write the canonical pkg/core+factory+app fixture used for symbol and call tests."""
    pkg = root / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text(
        '"""Core service."""\n\n\n'
        "class Service:\n"
        '    """Service docs."""\n\n'
        "    def create(self, name: str) -> str:\n"
        '        """Create docs."""\n'
        "        return name\n\n"
        "    def run(self, name: str) -> str:\n"
        "        return self.create(name)\n",
        encoding="utf-8",
    )
    (pkg / "factory.py").write_text(
        "from pkg.core import Service\n\n\n"
        "def make_service() -> Service:\n"
        '    """Make docs."""\n'
        "    return Service()\n",
        encoding="utf-8",
    )
    (pkg / "app.py").write_text(
        "import pkg.core as core\n"
        "from pkg.factory import make_service\n\n\n"
        "def main() -> str:\n"
        '    first = make_service().run("b")\n'
        "    svc = core.Service()\n"
        '    second = svc.create("a")\n'
        "    return first + second\n",
        encoding="utf-8",
    )


def index_symbol_repo(
    conn: sqlite3.Connection, tmp_path: Path, dirname: str = "symrepo"
) -> Path:
    """Write the symbol fixture and index it as demo 0.1."""
    from libref.services.code.code_ingest import index_repo

    root = tmp_path / dirname
    write_symbol_repo(root)
    index_repo(conn, "demo", "0.1", str(root), repos_dir=tmp_path / "repos")
    return root


def counting_client(handler, counter: list) -> httpx.AsyncClient:
    """Wrap a MockTransport handler and record request URLs."""

    def wrapped(request: httpx.Request) -> httpx.Response:
        counter.append(str(request.url))
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped))


class FakeVecStore:
    """In-memory vector backend for sync tests: tracks points plus ensured/deleted calls."""

    def __init__(self) -> None:
        self.points: dict[int, tuple] = {}
        self.seen: list[int] = []
        self.ensured: list[tuple] = []
        self.deleted: list[int] = []
        self.upserted: list[tuple] = []
        self.delete_version_calls: list[int] = []

    def ensure_collection(self, conn, model: str, dim: int) -> None:
        self.ensured.append((model, dim))

    def delete_docs(self, doc_ids: list[int]) -> None:
        self.deleted.extend(int(d) for d in doc_ids)
        for doc_id in doc_ids:
            self.points.pop(int(doc_id), None)

    def delete_version(self, version_id: int) -> None:
        self.delete_version_calls.append(int(version_id))
        for pid in [p for p, t in self.points.items() if t[2] == int(version_id)]:
            del self.points[pid]

    def upsert_batch(self, points, *, wait: bool = True) -> None:
        self.upserted.extend(points)
        for doc_id, library_id, version_id, embedding in points:
            self.points[int(doc_id)] = (int(doc_id), library_id, version_id, embedding)

    def count(self, version_id: int) -> int:
        return sum(1 for t in self.points.values() if t[2] == int(version_id))

    def list_ids(self, version_id: int, *, batch: int = 512) -> list[int]:
        return sorted(p for p, t in self.points.items() if t[2] == int(version_id))

    def search(self, query_vec, *, k: int, version_id: int):
        self.seen.append(int(version_id))
        return [(d, 1.0) for d in self.list_ids(version_id)[:k]]


async def fake_embed_vectors(texts: list[str]) -> list[list[float]]:
    """Deterministic stub embedding for sync tests: every non-empty text maps to one vector."""
    assert texts and all(t.strip() for t in texts)
    return [[1.0, 0.0] for _ in texts]


def run_coro(coro):
    """Bridge sync tests to async service entry points."""
    return asyncio.run(coro)


@contextlib.contextmanager
def mock_ingest(handler) -> Iterator[httpx.AsyncClient]:
    """Swap ingest._client for a MockTransport client; no network; restored and closed on exit."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    @contextlib.asynccontextmanager
    async def fake(_settings):
        yield client

    import libref.services.docs.ingest as mod

    original = mod._client
    mod._client = fake
    try:
        yield client
    finally:
        mod._client = original
        asyncio.run(client.aclose())
