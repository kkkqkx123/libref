"""Sync repair tests for the derived indexes: set verification, drift skip, FTS rebuild, purge tolerance."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from libref.chunking import Chunk
from libref.store.document_store import DocumentStore
from tests.conftest import make_crawler_settings, seed_library_version

pytestmark = pytest.mark.integration


class _FakeVec:
    def __init__(self) -> None:
        self.points: dict[int, tuple] = {}
        self.seen: list[int] = []

    def ensure_collection(self, conn, model: str, dim: int) -> None:
        return None

    def delete_docs(self, doc_ids: list[int]) -> None:
        for doc_id in doc_ids:
            self.points.pop(int(doc_id), None)

    def delete_version(self, version_id: int) -> None:
        for pid in [p for p, t in self.points.items() if t[2] == int(version_id)]:
            del self.points[pid]

    def upsert_batch(self, points, *, wait: bool = True) -> None:
        for doc_id, library_id, version_id, embedding in points:
            self.points[int(doc_id)] = (int(doc_id), library_id, version_id, embedding)

    def count(self, version_id: int) -> int:
        return sum(1 for t in self.points.values() if t[2] == int(version_id))

    def list_ids(self, version_id: int, *, batch: int = 512) -> list[int]:
        return sorted(p for p, t in self.points.items() if t[2] == int(version_id))

    def search(self, query_vec, *, k: int, version_id: int):
        self.seen.append(int(version_id))
        hits = [p for p in self.list_ids(version_id)[:k]]
        return [(d, 1.0) for d in hits]


async def _fake_embed(texts: list[str]) -> list[list[float]]:
    assert texts and all(t.strip() for t in texts)
    return [[1.0, 0.0] for _ in texts]


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
    asyncio.run(
        sync_vectors_for_docs(
            conn, settings, vec, _fake_embed, store, counts, [], [], version_id=1
        )
    )
    assert vec.list_ids(1) == kept.inserted_ids
    assert counts.get("vector_reconciled", 0) >= 1


def test_retriever_skips_drifted_version(conn, tmp_path, monkeypatch) -> None:
    from libref.store.retriever import HybridRetriever
    from libref.store.vec_store import write_version_meta

    monkeypatch.chdir(tmp_path)
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
            chunks=[
                Chunk(
                    url=url,
                    path="G",
                    title="G",
                    content="quetzaldrift shared body",
                    sort_order=0,
                )
            ],
            raw_content=url,
        )
    settings = make_crawler_settings(monkeypatch, tmp_path)
    write_version_meta(conn, 1, settings.embedding_model, settings.embedding_dim)
    write_version_meta(conn, 2, "other-model", settings.embedding_dim)
    vec = _FakeVec()
    vec.points[1] = (1, 1, 1, [1.0, 0.0])
    vec.points[2] = (2, 1, 2, [1.0, 0.0])

    async def _embed(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    results, degraded = asyncio.run(
        HybridRetriever(conn, settings, vec_store=vec, embed=_embed).search_async(
            "quetzaldrift", limit=8
        )
    )
    assert vec.seen == [1]
    assert degraded is True
    assert results


def test_fts_rebuild_repairs_direct_delete(conn, monkeypatch, tmp_path) -> None:
    from libref.services.docs.search import SearchService
    from libref.services.ops.maintenance import check_fts, rebuild_fts

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
                content="quetzalfts rebuild body",
                sort_order=0,
            )
        ],
        raw_content="a",
    )
    assert check_fts(conn)["in_sync"] == 1
    row = conn.execute(
        "SELECT title, path FROM documents WHERE id=?", (result.inserted_ids[0],)
    ).fetchone()
    assert (row[0], row[1]) == ("G", "G")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE documents SET title='hack' WHERE id=1")
    conn.rollback()
    conn.execute("INSERT INTO documents_fts(documents_fts) VALUES('delete-all')")
    conn.commit()
    drifted = check_fts(conn)
    assert drifted["in_sync"] == 0
    assert drifted["sampled"] >= 1
    assert drifted["missing"] >= 1
    assert SearchService(conn).search_docs("quetzalfts") == []
    out = rebuild_fts(conn)
    assert out["in_sync"] == 1
    assert check_fts(conn)["in_sync"] == 1
    assert len(SearchService(conn).search_docs("quetzalfts")) == 1


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
