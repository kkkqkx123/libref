"""QdrantVecStore end-to-end tests (real connection to localhost:6333).

Skipped by default; requires LIBREF_RUN_E2E=1 to enable. External-service
integration is validated on demand, not in the routine gate.

One minimal-preset collection per session, payload-scrubbed between tests, deleted
afterwards. Acceptance criteria: docs/plan/qdrant-migration.md §7.
"""

from __future__ import annotations

import os
import sqlite3
import uuid

import pytest
from qdrant_client import QdrantClient

from libref.db import apply_migrations, open_db
from libref.store.vec_store import QdrantVecStore

COLLECTION_PREFIX = "libref_test"
URL = "http://localhost:6333"
# Test client headroom: collection DDL with wait=true stalls under full-suite
# load on a tight box; the serving default stays 5.0s in production code.
_TEST_VERSIONS = (1, 10, 20)
_TEST_DIM = 4


def _server_up() -> bool:
    try:
        QdrantClient(url=URL, timeout=2.0, check_compatibility=False).get_collections()
        return True
    except Exception:  # noqa: BLE001 — liveness probe: any exception means the service is unreachable
        return False


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_qdrant,
    pytest.mark.skipif(not _server_up(), reason="Qdrant server unreachable"),
    pytest.mark.skipif(
        os.environ.get("LIBREF_RUN_E2E") != "1",
        reason="e2e requires LIBREF_RUN_E2E=1 and a local Qdrant",
    ),
]


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = open_db(tmp_path / "vec.db")
    apply_migrations(c)
    yield c
    c.close()


@pytest.fixture(scope="session")
def shared_collection() -> str:
    """One minimal-preset collection per session; pid plus random suffix isolates concurrent runs."""
    from qdrant_client import QdrantClient, models

    name = f"{COLLECTION_PREFIX}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    client = QdrantClient(url=URL, timeout=30.0)
    client.create_collection(
        collection_name=name,
        vectors_config=models.VectorParams(
            size=_TEST_DIM,
            distance=models.Distance.COSINE,
            hnsw_config=models.HnswConfigDiff(
                m=8, ef_construct=32, full_scan_threshold=100
            ),
            quantization_config=models.ScalarQuantization(
                scalar=models.ScalarQuantizationConfig(
                    type=models.ScalarType.INT8, quantile=0.99, always_ram=True
                )
            ),
        ),
    )
    client.create_payload_index(
        collection_name=name,
        field_name="version_id",
        field_schema=models.IntegerIndexParams(
            type=models.IntegerIndexType.INTEGER, is_principal=True
        ),
    )
    client.create_payload_index(
        collection_name=name,
        field_name="library_id",
        field_schema=models.PayloadSchemaType.INTEGER,
    )
    yield name
    if client.collection_exists(name):
        client.delete_collection(name)


def _scrub(store: QdrantVecStore) -> None:
    for version in _TEST_VERSIONS:
        store.delete_version(version)


@pytest.fixture()
def store(conn, shared_collection) -> QdrantVecStore:
    s = QdrantVecStore(URL, shared_collection, timeout=30.0)
    _scrub(s)
    yield s
    _scrub(s)


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def test_ensure_collection_and_meta(
    store: QdrantVecStore, conn, monkeypatch, tmp_path
) -> None:
    store.ensure_collection(conn, "BAAI/bge-small-zh", 4)
    assert store.client.collection_exists(store.collection)
    assert _meta(conn, "vec_model") == "BAAI/bge-small-zh"
    assert _meta(conn, "vec_dim") == "4"
    store.ensure_collection(conn, "BAAI/bge-small-zh", 4)  # idempotent


def test_upsert_search_version_filter(
    store: QdrantVecStore, conn, monkeypatch, tmp_path
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


def test_delete_docs_and_version(
    store: QdrantVecStore, conn, monkeypatch, tmp_path
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


def test_dim_change_invalidates(
    store: QdrantVecStore, conn, monkeypatch, tmp_path
) -> None:
    store.ensure_collection(conn, "m1", 4)
    store.upsert_batch([(1, 1, 10, [1.0, 0.0, 0.0, 0.0])])
    assert store.count(10) == 1

    store.ensure_collection(conn, "m1", 8)  # dim change → rebuild, new dim writable
    store.upsert_batch([(1, 1, 10, [1.0] * 8)])
    assert len(store.search([1.0] * 8, k=1, version_id=10)) == 1
    assert _meta(conn, "vec_dim") == "8"


def test_model_only_change_keeps_collection(
    store: QdrantVecStore, conn, monkeypatch, tmp_path
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


def test_list_ids_roundtrip(store: QdrantVecStore, conn, monkeypatch, tmp_path) -> None:
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


def test_reconcile_count_consistency(
    store: QdrantVecStore, conn, monkeypatch, tmp_path
) -> None:
    """Reconciliation baseline is the embeddable subset; bm25 chunks stay FTS-only."""
    import json as _json

    from libref.services.ops.maintenance import sqlite_embeddable_count

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
