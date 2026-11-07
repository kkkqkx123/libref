"""QdrantVecStore 集成测试（真连 localhost:6333，服务不可达则 skip）。

使用独立测试集合，结束后删除。验收标准见 docs/plan/qdrant-migration.md §7。
"""

from __future__ import annotations

import sqlite3

import pytest
from qdrant_client import QdrantClient

from libref.db import apply_migrations, open_db
from libref.store.vec_store import QdrantVecStore

COLLECTION = "libref_test_chunks"
URL = "http://localhost:6333"


def _server_up() -> bool:
    try:
        QdrantClient(url=URL, timeout=2.0).get_collections()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _server_up(), reason="Qdrant server unreachable")


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = open_db(tmp_path / "vec.db")
    apply_migrations(c)
    yield c
    c.close()


@pytest.fixture()
def store(conn) -> QdrantVecStore:
    s = QdrantVecStore(URL, COLLECTION)
    s.client.delete_collection(COLLECTION) if s.client.collection_exists(COLLECTION) else None
    yield s
    if s.client.collection_exists(COLLECTION):
        s.client.delete_collection(COLLECTION)


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def test_ensure_collection_and_meta(store: QdrantVecStore, conn) -> None:
    store.ensure_collection(conn, "BAAI/bge-small-zh", 4)
    assert store.client.collection_exists(COLLECTION)
    assert _meta(conn, "vec_model") == "BAAI/bge-small-zh"
    assert _meta(conn, "vec_dim") == "4"
    store.ensure_collection(conn, "BAAI/bge-small-zh", 4)  # 幂等


def test_upsert_search_version_filter(store: QdrantVecStore, conn) -> None:
    store.ensure_collection(conn, "m", 4)
    store.upsert_batch([
        (1, 1, 10, [1.0, 0.0, 0.0, 0.0]),   # 精确匹配，score 最高
        (2, 1, 10, [0.9, 0.1, 0.0, 0.0]),
        (3, 1, 20, [0.0, 0.0, 0.0, 1.0]),   # 其他版本，应被过滤
    ])
    results = store.search([1.0, 0.0, 0.0, 0.0], k=10, version_id=10)
    assert [r[0] for r in results][:1] == [1]
    assert all(results[i][1] >= results[i + 1][1] for i in range(len(results) - 1))
    assert len(results) == 2


def test_delete_docs_and_version(store: QdrantVecStore, conn) -> None:
    store.ensure_collection(conn, "m", 4)
    store.upsert_batch([
        (1, 1, 10, [1.0, 0.0, 0.0, 0.0]),
        (2, 1, 10, [0.0, 1.0, 0.0, 0.0]),
        (3, 1, 20, [0.0, 0.0, 1.0, 0.0]),
    ])
    store.delete_docs([1])
    assert store.count(10) == 1
    store.delete_version(10)
    assert store.count(10) == 0
    assert store.count(20) == 1  # 其他版本不受影响


def test_model_or_dim_change_invalidates(store: QdrantVecStore, conn) -> None:
    store.ensure_collection(conn, "m1", 4)
    store.upsert_batch([(1, 1, 10, [1.0, 0.0, 0.0, 0.0])])
    assert store.count(10) == 1

    store.ensure_collection(conn, "m2", 4)  # 换模型 → 重建失效
    assert store.count(10) == 0
    assert _meta(conn, "vec_model") == "m2"

    store.ensure_collection(conn, "m2", 8)  # 换维度 → 重建，可写入新维度
    store.upsert_batch([(1, 1, 10, [1.0] * 8)])
    assert len(store.search([1.0] * 8, k=1, version_id=10)) == 1
    assert _meta(conn, "vec_dim") == "8"


def test_reconcile_count_consistency(store: QdrantVecStore, conn) -> None:
    """对账语义：count(version) 应等于 SQLite documents 计数，差集即需重灌的部分。"""
    conn.execute("INSERT INTO libraries(name) VALUES('l')")
    conn.execute("INSERT INTO versions(library_id, version) VALUES(1, '1.0')")
    conn.execute("INSERT INTO pages(version_id, url) VALUES(1, 'https://e.com/1')")
    conn.executemany(
        "INSERT INTO documents(library_id, version_id, page_id, url, content,"
        " metadata, sort_order) VALUES(1, 1, 1, ?, 'doc', NULL, ?)",
        [(f"https://e.com/{i}", i) for i in range(5)],
    )
    conn.commit()
    store.ensure_collection(conn, "m", 4)
    store.upsert_batch([(i, 1, 1, [1.0, 0.0, 0.0, 0.0]) for i in range(1, 6)])
    sql_count = conn.execute(
        "SELECT count(*) FROM documents WHERE version_id=1"
    ).fetchone()[0]
    assert sql_count == 5
    assert store.count(1) == sql_count
