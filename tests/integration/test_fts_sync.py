"""FTS 触发器同步测试（P0 最小验证之一，蓝本 000-initial-schema 触发器语义）。"""

from __future__ import annotations

import json
import sqlite3

import pytest

pytestmark = pytest.mark.integration


def _add_library(conn: sqlite3.Connection) -> tuple[int, int]:
    conn.execute("INSERT INTO libraries(name) VALUES('lib')")
    lib_id = conn.execute("SELECT id FROM libraries").fetchone()[0]
    conn.execute(
        "INSERT INTO versions(library_id, version) VALUES(?, '1.0')", (lib_id,)
    )
    ver_id = conn.execute("SELECT id FROM versions").fetchone()[0]
    conn.execute(
        "INSERT INTO pages(version_id, url) VALUES(?, 'https://example.com/a')",
        (ver_id,),
    )
    page_id = conn.execute("SELECT id FROM pages").fetchone()[0]
    return ver_id, page_id


def _insert_doc(conn: sqlite3.Connection, page_id: int, content: str) -> int:
    cur = conn.execute(
        "INSERT INTO documents(library_id, version_id, page_id, url, content, metadata, sort_order)"
        " VALUES(1, 1, ?, 'https://example.com/a', ?, ?, 0)",
        (page_id, content, json.dumps({"title": "Getting Started", "path": "docs/start"})),
    )
    return cur.lastrowid


def _match(conn: sqlite3.Connection, query: str) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT rowid FROM documents_fts WHERE documents_fts MATCH ?", (query,)
    )]


def test_insert_visible_in_fts(conn: sqlite3.Connection) -> None:
    _, page_id = _add_library(conn)
    doc_id = _insert_doc(conn, page_id, "Use requests.get to fetch pages")
    conn.commit()
    assert _match(conn, "requests") == [doc_id]
    # metadata 的 title/path 列也可检索
    assert _match(conn, "title:getting") == [doc_id]
    assert _match(conn, "path:start") == [doc_id]


def test_update_resync(conn: sqlite3.Connection) -> None:
    _, page_id = _add_library(conn)
    doc_id = _insert_doc(conn, page_id, "alpha content")
    conn.commit()
    conn.execute("UPDATE documents SET content='beta content' WHERE id=?", (doc_id,))
    conn.commit()
    assert _match(conn, "alpha") == []
    assert _match(conn, "beta") == [doc_id]


def test_delete_removes_from_fts(conn: sqlite3.Connection) -> None:
    _, page_id = _add_library(conn)
    doc_id = _insert_doc(conn, page_id, "unique zebra term")
    conn.commit()
    conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    conn.commit()
    assert _match(conn, "zebra") == []


def test_fts_vocab_consistency(conn: sqlite3.Connection) -> None:
    """fts5vocab 视角：文档 token 与 FTS 索引一致。"""
    _, page_id = _add_library(conn)
    _insert_doc(conn, page_id, "alpha beta alpha")
    conn.commit()
    conn.execute(
        "CREATE VIRTUAL TABLE temp_fts_v USING fts5vocab('documents_fts', 'col')"
    )
    vocab = {
        r[0]
        for r in conn.execute("SELECT term FROM temp_fts_v WHERE col='content'")
    }
    conn.execute("DROP TABLE temp_fts_v")
    assert vocab == {"alpha", "beta"}
