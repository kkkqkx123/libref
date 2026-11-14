"""Vector sync wiring tests, all offline: collection derivation, embedded texts, unknown-shape rebuild, scrape sync, failure counting, FTS identifiers and ordering."""

from __future__ import annotations

import httpx
import pytest

from libref.chunking import Chunk
from libref.services.docs.ingest import IngestService
from libref.services.docs.search import SearchService
from libref.store.document_store import DocumentStore
from libref.store.vec_store import QdrantVecStore, collection_for
from libref.text.embedding import build_embed_text
from tests.conftest import (
    FakeVecStore as _FakeVec,
)
from tests.conftest import (
    fake_embed_vectors as _fake_embed,
)
from tests.conftest import (
    make_crawler_settings,
    mock_ingest,
    seed_library_version,
)

pytestmark = pytest.mark.integration


class _ShapeClient:
    """Minimal client implementing only what collection governance needs; shape set via constructor args."""

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


def test_collection_for_isolates_db_files(tmp_path, monkeypatch):
    a = collection_for("libref_chunks", tmp_path / "a.db")
    b = collection_for("libref_chunks", tmp_path / "b.db")
    assert a != b
    assert collection_for("libref_chunks", tmp_path / "a.db") == a
    assert a.startswith("libref_chunks_")
    assert collection_for("libref_chunks", ":memory:") == "libref_chunks"


def test_build_embed_text_variants(monkeypatch, tmp_path):
    assert build_embed_text("T", "P", "C") == "T / P\nC"
    assert build_embed_text(None, None, "C") == "C"
    assert build_embed_text("T", None, None) == "T\n"


def test_unknown_collection_shape_triggers_rebuild(conn, monkeypatch, tmp_path):
    client = _ShapeClient(vectors={"named": "unexpected"})
    store = QdrantVecStore("http://localhost:6333", "c", client=client)
    store.ensure_collection(conn, "m", 4)
    assert client.deleted == ["c"]
    assert len(client.created) == 1


def _handler(body: str):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path == "/llms.txt":
            return httpx.Response(
                200, text="- [Install](https://d.example/install.html)\n"
            )
        if request.url.path == "/install.html":
            return httpx.Response(200, text=body)
        if request.url.path == "/llms-full.txt":
            return httpx.Response(404)
        raise AssertionError(request.url.path)

    return handle


def test_scrape_syncs_vectors(conn, monkeypatch, tmp_path):
    body = (
        "<html><body><main><h1>Install</h1>"
        "<p>use uv to install libref quetzalvec</p></main></body></html>"
    )
    vec = _FakeVec()
    with mock_ingest(_handler(body)):
        counts = IngestService(
            make_crawler_settings(monkeypatch, tmp_path),
            conn,
            vec_store=vec,
            embed=_fake_embed,
        ).scrape_docs("docs", "1.0", "https://d.example/", max_pages=5)
    assert counts["stored"] == 1
    assert counts["vector_failed"] == 0
    assert vec.ensured and vec.deleted == []
    sql_ids = [r[0] for r in conn.execute("SELECT id FROM documents ORDER BY id")]
    assert sql_ids and [p[0] for p in vec.upserted] == sql_ids
    assert all(p[1] == 1 and p[2] == 1 and p[3] == [1.0, 0.0] for p in vec.upserted)


def test_rescrape_replaces_vectors(conn, monkeypatch, tmp_path):
    first = "<html><body><main><p>first body quetzalre</p></main></body></html>"
    second = "<html><body><main><p>second body quetzalre</p></main></body></html>"
    current = [first]

    def dynamic(request: httpx.Request) -> httpx.Response:
        return _handler(current[0])(request)

    vec = _FakeVec()
    with mock_ingest(dynamic):
        svc = IngestService(
            make_crawler_settings(monkeypatch, tmp_path),
            conn,
            vec_store=vec,
            embed=_fake_embed,
        )
        svc.scrape_docs("docs", "1.0", "https://d.example/", max_pages=5)
        old_points = list(vec.upserted)
        vec.upserted.clear()
        current[0] = second
        counts = svc.scrape_docs("docs", "1.0", "https://d.example/", max_pages=5)
    assert counts["stored"] == 1
    assert counts["vector_failed"] == 0
    assert sorted(vec.deleted) == sorted(p[0] for p in old_points)
    assert vec.upserted and {p[0] for p in vec.upserted}.isdisjoint(
        {p[0] for p in old_points}
    )


def test_embed_failure_counts_without_losing_truth(conn, monkeypatch, tmp_path):
    async def boom(texts: list[str]):
        raise RuntimeError("encoder down")

    vec = _FakeVec()
    with mock_ingest(
        _handler("<html><body><main><p>truth body quetzalboom</p></main></body></html>")
    ):
        counts = IngestService(
            make_crawler_settings(monkeypatch, tmp_path),
            conn,
            vec_store=vec,
            embed=boom,
        ).scrape_docs("docs", "1.0", "https://d.example/", max_pages=5)
    assert counts["stored"] == 1
    assert counts["vector_failed"] == 1
    assert SearchService(conn).search_docs("quetzalboom")


def test_search_results_carry_row_id(conn, monkeypatch, tmp_path):
    seed_library_version(conn)
    store = DocumentStore(conn)
    result = store.store_page(
        1,
        1,
        "https://d.example/a.html",
        "stored",
        chunks=[
            Chunk(
                url="https://d.example/a.html",
                path="G",
                title="G",
                content="quetzalid body",
                sort_order=0,
            )
        ],
        raw_content="b",
    )
    hits = SearchService(conn).search_docs("quetzalid")
    assert len(hits) == 1
    assert hits[0]["id"] == result.inserted_ids[0]


def test_reconcile_repairs_orphans_and_missing(conn, monkeypatch, tmp_path):
    import asyncio

    from libref.services.ops.maintenance import (
        embeddable_doc_ids,
        reconcile_version,
        sqlite_embeddable_count,
    )
    from libref.store.vec_store import write_version_meta

    seed_library_version(conn)
    store = DocumentStore(conn)
    kept = store.store_page(
        1,
        1,
        "https://d.example/a.html",
        "stored",
        chunks=[
            Chunk(
                url="https://d.example/a.html",
                path="G",
                title="G",
                content="quetzalrepair alpha",
                sort_order=0,
            )
        ],
        raw_content="a",
    )
    other = store.store_page(
        1,
        1,
        "https://d.example/b.html",
        "stored",
        chunks=[
            Chunk(
                url="https://d.example/b.html",
                path="G",
                title="G",
                content="quetzalrepair beta",
                sort_order=0,
                chunk_path="bm25",
            )
        ],
        raw_content="b",
    )
    settings = make_crawler_settings(monkeypatch, tmp_path)
    write_version_meta(conn, 1, settings.embedding_model, settings.embedding_dim)
    vec = _FakeVec()
    only_id = kept.inserted_ids[0]
    vec.points[only_id] = (only_id, 1, 1, [1.0, 0.0])
    vec.points[9999] = (9999, 1, 1, [1.0, 0.0])
    assert sqlite_embeddable_count(conn, 1) == 1
    assert embeddable_doc_ids(conn, 1) == [only_id]
    stats = asyncio.run(reconcile_version(conn, settings, vec, _fake_embed, 1))
    assert stats["missing"] == 0
    assert stats["orphans"] == 1
    assert stats["rebuilt"] == 0
    assert vec.list_ids(1) == [only_id]
    assert other.inserted_ids[0] not in vec.list_ids(1)


def test_model_only_drift_rebuilds_single_version(conn, monkeypatch, tmp_path):
    import asyncio

    from libref.services.ops.maintenance import reconcile_version

    seed_library_version(conn)
    conn.execute("INSERT INTO versions(id, library_id, version) VALUES(2, 1, '2.0')")
    conn.commit()
    store = DocumentStore(conn)
    for version_id, url in (
        (1, "https://d.example/a.html"),
        (2, "https://d.example/b.html"),
    ):
        store.store_page(
            version_id,
            1,
            url,
            "stored",
            chunks=[Chunk(url=url, path="G", title="G", content="body", sort_order=0)],
            raw_content=url,
        )
    settings = make_crawler_settings(monkeypatch, tmp_path)
    vec = _FakeVec()
    asyncio.run(reconcile_version(conn, settings, vec, _fake_embed, 1))
    asyncio.run(reconcile_version(conn, settings, vec, _fake_embed, 2))
    before_other = list(vec.list_ids(2))
    assert before_other
    settings = make_crawler_settings(
        monkeypatch, tmp_path, embedding_model="other-model"
    )
    stats = asyncio.run(reconcile_version(conn, settings, vec, _fake_embed, 1))
    assert stats["rebuilt"] == 1
    assert vec.delete_version_calls[-1] == 1
    assert vec.list_ids(2) == before_other


def test_purge_clears_vectors_after_truth_commit(conn, monkeypatch, tmp_path):
    from libref.services.ops.retention import purge_version

    seed_library_version(conn)
    store = DocumentStore(conn)
    store.store_page(
        1,
        1,
        "https://d.example/a.html",
        "stored",
        chunks=[
            Chunk(
                url="https://d.example/a.html",
                path="G",
                title="G",
                content="quetzalpurge body",
                sort_order=0,
            )
        ],
        raw_content="a",
    )
    vec = _FakeVec()
    vec.points[1] = (1, 1, 1, [1.0, 0.0])
    out = purge_version(conn, "docs", "1.0", vec_store=vec)
    assert out["removed_docs"] == 1
    assert vec.delete_version_calls == [1, 1]
    assert vec.deleted == []
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0


def test_title_match_outranks_content_only(conn, monkeypatch, tmp_path):
    seed_library_version(conn)
    store = DocumentStore(conn)
    store.store_page(
        1,
        1,
        "https://d.example/a.html",
        "stored",
        chunks=[
            Chunk(
                url="https://d.example/a.html",
                path="Guide",
                title="Install Guide",
                content="unrelated pineapple text",
                sort_order=0,
            )
        ],
        raw_content="a",
    )
    store.store_page(
        1,
        1,
        "https://d.example/b.html",
        "stored",
        chunks=[
            Chunk(
                url="https://d.example/b.html",
                path="Other",
                title="Other",
                content="install libref now",
                sort_order=0,
            )
        ],
        raw_content="b",
    )
    hits = SearchService(conn).search_docs("install")
    assert [h["title"] for h in hits] == ["Install Guide", "Other"]


def test_verify_catches_same_count_divergence(conn, monkeypatch, tmp_path) -> None:
    from libref.services.ops.maintenance import sync_vectors_for_docs
    from libref.store.vec_store import write_version_meta

    seed_library_version(conn)
    store = DocumentStore(conn)
    kept = store.store_page(
        1,
        1,
        "https://d.example/a.html",
        "stored",
        chunks=[
            Chunk(
                url="https://d.example/a.html",
                path="G",
                title="G",
                content="quetzalverify alpha",
                sort_order=0,
            )
        ],
        raw_content="a",
    )
    settings = make_crawler_settings(monkeypatch, tmp_path)
    write_version_meta(conn, 1, settings.embedding_model, settings.embedding_dim)
    vec = _FakeVec()
    vec.points[9999] = (9999, 1, 1, [1.0, 0.0])
    assert vec.count(1) == 1
    counts: dict[str, int] = {}
    import asyncio

    asyncio.run(
        sync_vectors_for_docs(
            conn, settings, vec, _fake_embed, store, counts, [], [], version_id=1
        )
    )
    assert vec.list_ids(1) == kept.inserted_ids
    assert counts.get("vector_reconciled", 0) >= 1


def test_purge_deletes_truth_when_vectors_fail(conn, monkeypatch, tmp_path) -> None:
    from libref.services.ops.retention import purge_version

    seed_library_version(conn)
    store = DocumentStore(conn)
    store.store_page(
        1,
        1,
        "https://d.example/a.html",
        "stored",
        chunks=[
            Chunk(
                url="https://d.example/a.html",
                path="G",
                title="G",
                content="quetzalpurge fail body",
                sort_order=0,
            )
        ],
        raw_content="a",
    )

    class _Boom:
        def delete_version(self, version_id: int) -> None:
            raise RuntimeError("vector down")

    out = purge_version(conn, "docs", "1.0", vec_store=_Boom())
    assert out["removed_docs"] == 1
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0


def test_version_drifted_never_embedded_is_current(conn, monkeypatch, tmp_path) -> None:
    from libref.services.ops.maintenance import version_drifted
    from libref.store.vec_store import write_version_meta

    seed_library_version(conn)
    settings = make_crawler_settings(monkeypatch, tmp_path)
    assert version_drifted(conn, settings, 1) is False
    write_version_meta(conn, 1, settings.embedding_model, settings.embedding_dim)
    assert version_drifted(conn, settings, 1) is False
    write_version_meta(conn, 1, "other-model", settings.embedding_dim)
    assert version_drifted(conn, settings, 1) is True
