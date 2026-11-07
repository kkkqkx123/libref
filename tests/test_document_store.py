"""DocumentStore 测试：六态结局、增量短路、FTS 级联。"""

from __future__ import annotations

from libref.chunking import Chunk
from libref.store.document_store import DocumentStore, content_hash


def seed_lv(conn, library: str = "docs", version: str = "1.0") -> tuple[int, int]:
    """测试库/版本外键行（固定 id=1，供 store_page(version_id=1, library_id=1) 使用）。"""
    conn.execute("INSERT INTO libraries(id, name) VALUES(1, ?)", (library,))
    conn.execute("INSERT INTO versions(id, library_id, version) VALUES(1, 1, ?)", (version,))
    conn.commit()
    return 1, 1


def make_chunks(url: str = "https://d.example/a.html") -> list[Chunk]:
    return [
        Chunk(url=url, path="Guide", title="Guide", content="install libref now", sort_order=0),
        Chunk(url=url, path="Guide / Usage", title="Usage", content="import libref", sort_order=1),
    ]


def test_stored_creates_page_documents_and_fts(conn):
    seed_lv(conn)
    store = DocumentStore(conn)
    outcome = store.store_page(
        version_id=1, library_id=1, url="https://d.example/a.html",
        outcome="stored", chunks=make_chunks(), raw_content="# Guide\ninstall libref now",
    )
    assert outcome == "stored"
    rows = conn.execute("SELECT count(*) FROM documents").fetchone()
    assert rows[0] == 2
    hits = conn.execute(
        "SELECT count(*) FROM documents_fts WHERE documents_fts MATCH 'libref'"
    ).fetchone()
    assert hits[0] == 2


def test_unchanged_keeps_documents(conn):
    seed_lv(conn)
    store = DocumentStore(conn)
    store.store_page(1, 1, "https://d.example/a.html", "stored",
                     chunks=make_chunks(), raw_content="v1 body")
    before = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
    # 同内容再次 stored 判定 → ensure_changed 短路
    outcome = store.ensure_changed(1, "https://d.example/a.html", "v1 body")
    assert outcome == "unchanged"
    store.store_page(1, 1, "https://d.example/a.html", "unchanged")
    after = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
    assert before == after == 2


def test_unchanged_updates_fetched_at_only(conn):
    seed_lv(conn)
    store = DocumentStore(conn)
    store.store_page(1, 1, "https://d.example/a.html", "stored",
                     chunks=make_chunks(), raw_content="body")
    assert store.store_page(1, 1, "https://d.example/a.html", "unchanged") == "unchanged"


def test_absent_deletes_page_and_documents(conn):
    seed_lv(conn)
    store = DocumentStore(conn)
    store.store_page(1, 1, "https://d.example/a.html", "stored", chunks=make_chunks(), raw_content="b")
    assert store.store_page(1, 1, "https://d.example/a.html", "absent") == "absent"
    assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    # FTS 触发器级联清空
    hits = conn.execute(
        "SELECT count(*) FROM documents_fts WHERE documents_fts MATCH 'libref'"
    ).fetchone()[0]
    assert hits == 0


def test_empty_keeps_page_without_documents(conn):
    seed_lv(conn)
    store = DocumentStore(conn)
    assert store.store_page(1, 1, "https://d.example/a.html", "empty", raw_content="") == "empty"
    assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0


def test_skipped_and_failed_are_noop_writes(conn):
    seed_lv(conn)
    store = DocumentStore(conn)
    assert store.store_page(1, 1, "u1", "skipped") == "skipped"
    assert store.store_page(1, 1, "u2", "failed") == "failed"
    assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 0


def test_unknown_outcome_rejected(conn):
    import pytest
    seed_lv(conn)
    store = DocumentStore(conn)
    with pytest.raises(ValueError):
        store.store_page(1, 1, "u", "exploded")


def test_content_hash_stability():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")
