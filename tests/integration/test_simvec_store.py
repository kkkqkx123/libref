"""SimvecVecStore integration tests (built libsimvec, skip when absent).

Mirrors the Qdrant backend acceptance criteria on the shared six-method
surface, plus factory dispatch for both backends.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from libref.db import apply_migrations, open_db
from libref.services.factory import build_vector
from libref.store.vec_store import (
    QdrantVecStore,
    SimvecVecStore,
    close_simvec_engines,
    simvec_available,
    simvec_root_for,
)
from tests.conftest import make_settings

COLLECTION = "libref_test_chunks"

needs_lib = pytest.mark.skipif(not simvec_available(), reason="libsimvec not built")


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = open_db(tmp_path / "vec.db")
    apply_migrations(c)
    yield c
    c.close()


@pytest.fixture()
def store(tmp_path) -> SimvecVecStore:
    s = SimvecVecStore(tmp_path / "engine", COLLECTION)
    yield s
    close_simvec_engines()


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _gateway():
    async def _embed(texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    return SimpleNamespace(embed_fn=lambda: _embed)


@pytest.mark.integration
@pytest.mark.requires_simvec
@needs_lib
def test_simvec_root_derivation(tmp_path, monkeypatch) -> None:
    assert simvec_root_for(tmp_path / "a.db").name == "a.db.simvec"
    assert simvec_root_for(":memory:").exists()
    assert (
        simvec_root_for(tmp_path / "a.db", tmp_path / "custom") == tmp_path / "custom"
    )


@pytest.mark.integration
@pytest.mark.requires_simvec
@needs_lib
def test_ensure_collection_and_meta(
    store: SimvecVecStore, conn, monkeypatch, tmp_path
) -> None:
    store.ensure_collection(conn, "BAAI/bge-small-zh", 4)
    assert store._engine.collection_exists(COLLECTION)
    assert _meta(conn, "vec_model") == "BAAI/bge-small-zh"
    assert _meta(conn, "vec_dim") == "4"
    store.ensure_collection(conn, "BAAI/bge-small-zh", 4)  # idempotent


@pytest.mark.integration
@pytest.mark.requires_simvec
@needs_lib
def test_upsert_search_version_filter(
    store: SimvecVecStore, conn, monkeypatch, tmp_path
) -> None:
    store.ensure_collection(conn, "m", 4)
    store.upsert_batch(
        [
            (1, 1, 10, [1.0, 0.0, 0.0, 0.0]),  # exact match, highest score
            (2, 1, 10, [0.9, 0.1, 0.0, 0.0]),
            (3, 1, 20, [0.0, 0.0, 0.0, 1.0]),  # other version, should be filtered
        ]
    )
    results = store.search([1.0, 0.0, 0.0, 0.0], k=10, version_id=10)
    assert [r[0] for r in results][:1] == [1]
    assert all(results[i][1] >= results[i + 1][1] for i in range(len(results) - 1))
    assert len(results) == 2


@pytest.mark.integration
@pytest.mark.requires_simvec
@needs_lib
def test_delete_docs_and_version(
    store: SimvecVecStore, conn, monkeypatch, tmp_path
) -> None:
    store.ensure_collection(conn, "m", 4)
    store.upsert_batch(
        [
            (1, 1, 10, [1.0, 0.0, 0.0, 0.0]),
            (2, 1, 10, [0.0, 1.0, 0.0, 0.0]),
            (3, 1, 20, [0.0, 0.0, 1.0, 0.0]),
        ]
    )
    store.delete_docs([1])
    assert store.count(10) == 1
    store.delete_version(10)
    assert store.count(10) == 0
    assert store.count(20) == 1  # other versions unaffected


@pytest.mark.integration
@pytest.mark.requires_simvec
@needs_lib
def test_dim_change_invalidates(
    store: SimvecVecStore, conn, monkeypatch, tmp_path
) -> None:
    store.ensure_collection(conn, "m1", 4)
    store.upsert_batch([(1, 1, 10, [1.0, 0.0, 0.0, 0.0])])
    assert store.count(10) == 1

    store.ensure_collection(conn, "m1", 8)  # dim change → rebuild, new dim writable
    store.upsert_batch([(1, 1, 10, [1.0] * 8)])
    assert len(store.search([1.0] * 8, k=1, version_id=10)) == 1
    assert _meta(conn, "vec_dim") == "8"


@pytest.mark.integration
@pytest.mark.requires_simvec
@needs_lib
def test_model_only_change_keeps_collection(
    store: SimvecVecStore, conn, monkeypatch, tmp_path
) -> None:
    """Model-name drift never wipes other versions; per-version repair owns it."""
    store.ensure_collection(conn, "m1", 4)
    store.upsert_batch(
        [
            (1, 1, 10, [1.0, 0.0, 0.0, 0.0]),
            (2, 1, 20, [0.0, 1.0, 0.0, 0.0]),
        ]
    )
    store.ensure_collection(conn, "m2", 4)
    assert store.count(10) == 1
    assert store.count(20) == 1
    assert _meta(conn, "vec_model") == "m2"


@pytest.mark.integration
@pytest.mark.requires_simvec
@needs_lib
def test_list_ids_roundtrip(store: SimvecVecStore, conn, monkeypatch, tmp_path) -> None:
    store.ensure_collection(conn, "m", 4)
    store.upsert_batch(
        [
            (1, 1, 10, [1.0, 0.0, 0.0, 0.0]),
            (2, 1, 10, [0.0, 1.0, 0.0, 0.0]),
            (3, 1, 20, [0.0, 0.0, 1.0, 0.0]),
        ]
    )
    assert store.list_ids(10) == [1, 2]
    assert store.list_ids(20) == [3]
    assert store.list_ids(99) == []


@pytest.mark.integration
@pytest.mark.requires_simvec
@needs_lib
def test_reconcile_count_consistency(
    store: SimvecVecStore, conn, monkeypatch, tmp_path
) -> None:
    """Reconciliation baseline is the embeddable subset; bm25 chunks stay FTS-only."""
    import json as _json

    from libref.services.maintenance import sqlite_embeddable_count

    conn.execute("INSERT INTO libraries(name) VALUES('l')")
    conn.execute("INSERT INTO versions(library_id, version) VALUES(1, '1.0')")
    conn.execute("INSERT INTO pages(version_id, url) VALUES(1, 'https://e.com/1')")
    conn.executemany(
        "INSERT INTO documents(library_id, version_id, page_id, url, content,"
        " metadata, sort_order) VALUES(1, 1, 1, ?, 'doc', NULL, ?)",
        [(f"https://e.com/{i}", i) for i in range(5)],
    )
    conn.execute(
        "INSERT INTO documents(library_id, version_id, page_id, url, content,"
        " metadata, sort_order) VALUES(1, 1, 1, 'https://e.com/bm25', 'doc', ?, 5)",
        (_json.dumps({"chunk_path": "bm25"}),),
    )
    conn.commit()
    store.ensure_collection(conn, "m", 4)
    store.upsert_batch([(i, 1, 1, [1.0, 0.0, 0.0, 0.0]) for i in range(1, 6)])
    assert (
        conn.execute("SELECT count(*) FROM documents WHERE version_id=1").fetchone()[0]
        == 6
    )
    assert sqlite_embeddable_count(conn, 1) == 5
    assert store.count(1) == sqlite_embeddable_count(conn, 1)


@pytest.mark.integration
def test_build_vector_dispatch(tmp_path, monkeypatch) -> None:
    """Factory returns the configured backend; the remote choice needs no server."""
    remote = make_settings(
        monkeypatch, tmp_path, db_path=tmp_path / "r.db", vector_backend="qdrant"
    )
    vec, _, _ = build_vector(remote, gateway=_gateway())
    assert isinstance(vec, QdrantVecStore)


@pytest.mark.integration
@pytest.mark.requires_simvec
@needs_lib
def test_build_vector_dispatch_simvec(tmp_path, monkeypatch) -> None:
    local = make_settings(
        monkeypatch, tmp_path, db_path=tmp_path / "l.db", vector_backend="simvec"
    )
    vec, _, _ = build_vector(local, gateway=_gateway())
    try:
        assert isinstance(vec, SimvecVecStore)
    finally:
        close_simvec_engines()
