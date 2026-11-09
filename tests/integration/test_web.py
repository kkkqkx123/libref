"""Web 出口集成测试：健康、检索、读取、审核闭环，不出网。"""

from __future__ import annotations

import os
import sqlite3

import pytest

from libref.chunking import Chunk
from libref.config import LibrefSettings
from libref.db import apply_migrations, open_db
from libref.store.document_store import DocumentStore
from libref.web import build_web_app

pytestmark = pytest.mark.integration


def _open(tmp_path, name: str = "web.db") -> sqlite3.Connection:
    db_path = tmp_path / name
    conn = open_db(db_path, check_same_thread=False)
    apply_migrations(conn)
    return conn


def _seed(conn) -> None:
    conn.execute("INSERT INTO libraries(id, name) VALUES(1, 'docs')")
    conn.execute("INSERT INTO versions(id, library_id, version) VALUES(1, 1, '1.0')")
    conn.commit()
    store = DocumentStore(conn)
    store.store_page(
        1, 1, "https://d.example/w.html", "stored",
        chunks=[Chunk(url="https://d.example/w.html", path="G", title="Guide",
                      content="quetzalweb guide body", sort_order=0)],
        raw_content="quetzalweb guide body",
    )


def _client(conn, **overrides):
    for name in [n for n in os.environ if n.startswith("LIBREF_")]:
        os.environ.pop(name)
    settings = LibrefSettings(**overrides)
    app = build_web_app(conn, settings)
    from fastapi.testclient import TestClient

    return TestClient(app)


def test_health_and_search_and_read(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    conn = _open(tmp_path)
    try:
        _seed(conn)
        client = _client(conn)
        assert client.get("/health").json()["ok"] is True
        res = client.get("/search", params={"query": "quetzalweb"})
        assert res.status_code == 200
        assert res.json()["count"] == 1
        page = client.get("/read/page", params={"url": "https://d.example/w.html"})
        assert page.status_code == 200
        assert "quetzalweb" in page.json()["content"]
        assert page.json()["chunk_details"][0]["source_group_id"] == ""
    finally:
        conn.close()


def test_review_flow(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    conn = _open(tmp_path, "review.db")
    try:
        conn.execute("INSERT INTO libraries(id, name) VALUES(1, 'docs')")
        conn.execute("INSERT INTO versions(id, library_id, version) VALUES(1, 1, '1.0')")
        cur = conn.execute(
            "INSERT INTO call_instances(call_uid, callee_name, args, repo, caller_file,"
            " line_start, line_end, confidence, library_id, version_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("uid-web-1", "m.g", "[]", "r", "a.py", 1, 2, "AMBIGUOUS", 1, 1),
        )
        call_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO review_status(entity_type, entity_id, status)"
            " VALUES('call_instance', ?, 'pending')",
            (call_id,),
        )
        conn.commit()
        client = _client(conn)
        pending = client.get("/review/pending").json()
        assert pending["count"] == 1
        assert client.post("/review", json={"entity_id": call_id, "action": "approve"}).json() == {"ok": True}
        assert client.get("/review/pending").json()["count"] == 0
    finally:
        conn.close()


def test_unauthorized_when_token_set(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    conn = _open(tmp_path, "auth.db")
    try:
        _seed(conn)
        client = _client(conn, mcp_token="tok-secret")
        assert client.get("/libraries").status_code == 401
        assert client.get("/libraries", headers={"Authorization": "Bearer tok-secret"}).status_code == 200
    finally:
        conn.close()
