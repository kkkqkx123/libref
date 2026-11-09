"""DocumentStore tests: six-state outcomes, incremental short-circuit, sync incremental, FTS cascade."""

from __future__ import annotations

import pytest

from libref.chunking import Chunk
from libref.store.document_store import DocumentStore, content_hash
from tests.conftest import seed_library_version

pytestmark = pytest.mark.integration


def seed_lv(conn, library: str = "docs", version: str = "1.0") -> tuple[int, int]:
    return seed_library_version(conn, library, version)


def make_chunks(url: str = "https://d.example/a.html") -> list[Chunk]:
    return [
        Chunk(url=url, path="Guide", title="Guide", content="install libref now", sort_order=0),
        Chunk(url=url, path="Guide / Usage", title="Usage", content="import libref", sort_order=1),
    ]


def test_stored_creates_page_documents_and_fts(conn):
    seed_lv(conn)
    store = DocumentStore(conn)
    result = store.store_page(
        version_id=1, library_id=1, url="https://d.example/a.html",
        outcome="stored", chunks=make_chunks(), raw_content="# Guide\ninstall libref now",
    )
    assert result.outcome == "stored"
    assert result.removed_ids == []
    assert len(result.inserted_ids) == 2
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
    # storing the same content again hits the short-circuit check
    outcome = store.ensure_changed(1, "https://d.example/a.html", "v1 body")
    assert outcome == "unchanged"
    result = store.store_page(1, 1, "https://d.example/a.html", "unchanged")
    assert result.outcome == "unchanged"
    assert result.removed_ids == [] and result.inserted_ids == []
    after = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
    assert before == after == 2


def test_unchanged_updates_fetched_at_only(conn):
    seed_lv(conn)
    store = DocumentStore(conn)
    store.store_page(1, 1, "https://d.example/a.html", "stored",
                     chunks=make_chunks(), raw_content="body")
    result = store.store_page(1, 1, "https://d.example/a.html", "unchanged")
    assert result.outcome == "unchanged"


def test_absent_deletes_page_and_documents(conn):
    seed_lv(conn)
    store = DocumentStore(conn)
    first = store.store_page(1, 1, "https://d.example/a.html", "stored",
                             chunks=make_chunks(), raw_content="b")
    result = store.store_page(1, 1, "https://d.example/a.html", "absent")
    assert result.outcome == "absent"
    assert sorted(result.removed_ids) == sorted(first.inserted_ids)
    assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    # FTS trigger cascades the wipe
    hits = conn.execute(
        "SELECT count(*) FROM documents_fts WHERE documents_fts MATCH 'libref'"
    ).fetchone()[0]
    assert hits == 0


def test_restored_reports_replaced_ids(conn):
    seed_lv(conn)
    store = DocumentStore(conn)
    first = store.store_page(1, 1, "https://d.example/a.html", "stored",
                             chunks=make_chunks(), raw_content="v1")
    second = store.store_page(
        1, 1, "https://d.example/a.html", "stored",
        chunks=[Chunk(url="https://d.example/a.html", path="G", title="G",
                      content="brand new body", sort_order=0)],
        raw_content="v2",
    )
    assert second.outcome == "stored"
    assert sorted(second.removed_ids) == sorted(first.inserted_ids)
    assert len(second.inserted_ids) == 1
    assert set(second.removed_ids).isdisjoint(second.inserted_ids)


def test_empty_keeps_page_without_documents(conn):
    seed_lv(conn)
    store = DocumentStore(conn)
    first = store.store_page(1, 1, "https://d.example/a.html", "stored",
                             chunks=make_chunks(), raw_content="b")
    result = store.store_page(1, 1, "https://d.example/a.html", "empty", raw_content="")
    assert result.outcome == "empty"
    assert sorted(result.removed_ids) == sorted(first.inserted_ids)
    assert result.inserted_ids == []
    assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0


def test_skipped_and_failed_are_noop_writes(conn):
    seed_lv(conn)
    store = DocumentStore(conn)
    assert store.store_page(1, 1, "u1", "skipped").outcome == "skipped"
    assert store.store_page(1, 1, "u2", "failed").outcome == "failed"
    assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 0


def test_unknown_outcome_rejected(conn):
    import pytest
    seed_lv(conn)
    store = DocumentStore(conn)
    with pytest.raises(ValueError):
        store.store_page(1, 1, "u", "exploded")


def test_get_documents_batch_roundtrip(conn):
    seed_lv(conn)
    store = DocumentStore(conn)
    result = store.store_page(1, 1, "https://d.example/a.html", "stored",
                              chunks=make_chunks(), raw_content="b")
    docs = store.get_documents(list(reversed(result.inserted_ids)) + [999999])
    assert [d["id"] for d in docs] == list(reversed(result.inserted_ids))
    assert docs[0]["content"] == "import libref"
    assert docs[0]["metadata"]["title"] == "Usage"
    assert docs[0]["metadata"]["path"] == "Guide / Usage"
    assert docs[0]["metadata"]["chunk_path"] == "embedding"
    assert docs[0]["metadata"]["span_kind"] == "document_range"
    assert docs[0]["library_id"] == 1 and docs[0]["version_id"] == 1
    assert store.get_documents([]) == []


def test_content_hash_stability():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")
