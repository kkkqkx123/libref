"""向量同步接线测试，全部离线：集合派生、编码文本、未知形态重建、抓取同步、失败计数、全文标识与排序。"""

from __future__ import annotations

import asyncio
import contextlib

import httpx

from libref.chunking import Chunk
from libref.config import LibrefSettings
from libref.services.ingest import IngestService
from libref.services.search import SearchService
from libref.store.document_store import DocumentStore
from libref.store.vec_store import QdrantVecStore, build_embed_text, collection_for


class _ShapeClient:
    """只实现集合治理所需的最小客户端，形态由构造参数决定。"""

    def __init__(self, vectors) -> None:
        self._vectors = vectors
        self._alive = True
        self.deleted: list[str] = []
        self.created: list[dict] = []

    def collection_exists(self, name: str) -> bool:
        return self._alive

    def get_collection(self, name: str):
        from types import SimpleNamespace
        return SimpleNamespace(
            config=SimpleNamespace(params=SimpleNamespace(vectors=self._vectors))
        )

    def delete_collection(self, name: str) -> None:
        self.deleted.append(name)
        self._alive = False

    def create_collection(self, **kwargs) -> None:
        self.created.append(kwargs)
        self._alive = True

    def create_payload_index(self, **kwargs) -> None:
        pass


class _FakeVec:
    def __init__(self) -> None:
        self.ensured: list[tuple] = []
        self.deleted: list[int] = []
        self.upserted: list[tuple] = []

    def ensure_collection(self, conn, model: str, dim: int) -> None:
        self.ensured.append((model, dim))

    def delete_docs(self, doc_ids: list[int]) -> None:
        self.deleted.extend(doc_ids)

    def upsert_batch(self, points, *, wait: bool = True) -> None:
        self.upserted.extend(points)


async def _fake_embed(texts: list[str]) -> list[list[float]]:
    assert texts and all(t.strip() for t in texts)
    return [[1.0, 0.0] for _ in texts]


def test_collection_for_isolates_db_files(tmp_path):
    a = collection_for("libref_chunks", tmp_path / "a.db")
    b = collection_for("libref_chunks", tmp_path / "b.db")
    assert a != b
    assert collection_for("libref_chunks", tmp_path / "a.db") == a
    assert a.startswith("libref_chunks_")
    assert collection_for("libref_chunks", ":memory:") == "libref_chunks"


def test_build_embed_text_variants():
    assert build_embed_text("T", "P", "C") == "T / P\nC"
    assert build_embed_text(None, None, "C") == "C"
    assert build_embed_text("T", None, None) == "T\n"


def test_unknown_collection_shape_triggers_rebuild(conn):
    client = _ShapeClient(vectors={"named": "unexpected"})
    store = QdrantVecStore("http://localhost:6333", "c", client=client)
    store.ensure_collection(conn, "m", 4)
    assert client.deleted == ["c"]
    assert len(client.created) == 1


def _seed_lv(conn):
    conn.execute("INSERT INTO libraries(id, name) VALUES(1, 'docs')")
    conn.execute("INSERT INTO versions(id, library_id, version) VALUES(1, 1, '1.0')")
    conn.commit()


def _patch_client(handler):
    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    @contextlib.asynccontextmanager
    async def fake_client(_settings):
        yield mock

    import libref.services.ingest as ingest_mod
    original = ingest_mod._client
    ingest_mod._client = fake_client

    def restore():
        ingest_mod._client = original
        asyncio.run(mock.aclose())

    return restore


def _handler(body: str):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path == "/llms.txt":
            return httpx.Response(200, text="- [Install](https://d.example/install.html)\n")
        if request.url.path == "/install.html":
            return httpx.Response(200, text=body)
        raise AssertionError(request.url.path)
    return handle


def _settings():
    return LibrefSettings(
        request_timeout=5.0, fetch_concurrency=2, crawl_delay_default=0.0,
        robots_cache_ttl_days=1.0,
    )


def test_scrape_syncs_vectors(conn):
    body = ("<html><body><main><h1>Install</h1>"
            "<p>use uv to install libref quetzalvec</p></main></body></html>")
    restore = _patch_client(_handler(body))
    try:
        vec = _FakeVec()
        counts = IngestService(_settings(), conn, vec_store=vec,
                               embed=_fake_embed).scrape_docs(
            "docs", "1.0", "https://d.example/", max_pages=5)
    finally:
        restore()
    assert counts["stored"] == 1
    assert counts["vector_failed"] == 0
    assert vec.ensured and vec.deleted == []
    sql_ids = [r[0] for r in conn.execute("SELECT id FROM documents ORDER BY id")]
    assert sql_ids and [p[0] for p in vec.upserted] == sql_ids
    assert all(p[1] == 1 and p[2] == 1 and p[3] == [1.0, 0.0] for p in vec.upserted)


def test_rescrape_replaces_vectors(conn):
    first = ("<html><body><main><p>first body quetzalre</p></main></body></html>")
    second = ("<html><body><main><p>second body quetzalre</p></main></body></html>")
    current = [first]

    def dynamic(request: httpx.Request) -> httpx.Response:
        return _handler(current[0])(request)

    restore = _patch_client(dynamic)
    try:
        vec = _FakeVec()
        svc = IngestService(_settings(), conn, vec_store=vec, embed=_fake_embed)
        svc.scrape_docs("docs", "1.0", "https://d.example/", max_pages=5)
        old_points = list(vec.upserted)
        vec.upserted.clear()
        current[0] = second
        counts = svc.scrape_docs("docs", "1.0", "https://d.example/", max_pages=5)
    finally:
        restore()
    assert counts["stored"] == 1
    assert counts["vector_failed"] == 0
    assert sorted(vec.deleted) == sorted(p[0] for p in old_points)
    assert vec.upserted and {p[0] for p in vec.upserted}.isdisjoint(
        {p[0] for p in old_points})


def test_embed_failure_counts_without_losing_truth(conn):
    async def boom(texts: list[str]):
        raise RuntimeError("encoder down")

    restore = _patch_client(_handler("<html><body><main><p>truth body quetzalboom</p></main></body></html>"))
    try:
        vec = _FakeVec()
        counts = IngestService(_settings(), conn, vec_store=vec,
                               embed=boom).scrape_docs(
            "docs", "1.0", "https://d.example/", max_pages=5)
    finally:
        restore()
    assert counts["stored"] == 1
    assert counts["vector_failed"] == 1
    assert SearchService(conn).search_docs("quetzalboom")


def test_search_results_carry_row_id(conn):
    _seed_lv(conn)
    store = DocumentStore(conn)
    result = store.store_page(
        1, 1, "https://d.example/a.html", "stored",
        chunks=[Chunk(url="https://d.example/a.html", path="G", title="G",
                      content="quetzalid body", sort_order=0)],
        raw_content="b",
    )
    hits = SearchService(conn).search_docs("quetzalid")
    assert len(hits) == 1
    assert hits[0]["id"] == result.inserted_ids[0]


def test_title_match_outranks_content_only(conn):
    _seed_lv(conn)
    store = DocumentStore(conn)
    store.store_page(
        1, 1, "https://d.example/a.html", "stored",
        chunks=[Chunk(url="https://d.example/a.html", path="Guide", title="Install Guide",
                      content="unrelated pineapple text", sort_order=0)],
        raw_content="a",
    )
    store.store_page(
        1, 1, "https://d.example/b.html", "stored",
        chunks=[Chunk(url="https://d.example/b.html", path="Other", title="Other",
                      content="install libref now", sort_order=0)],
        raw_content="b",
    )
    hits = SearchService(conn).search_docs("install")
    assert [h["title"] for h in hits] == ["Install Guide", "Other"]
