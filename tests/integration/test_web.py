"""Web endpoint integration tests: health, search, read, review loop, no network."""

from __future__ import annotations

import sqlite3

import pytest

from libref.chunking import Chunk
from libref.db import apply_migrations, open_db
from libref.store.document_store import DocumentStore
from libref.web import build_web_app
from tests.conftest import make_settings

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
        1,
        1,
        "https://d.example/w.html",
        "stored",
        chunks=[
            Chunk(
                url="https://d.example/w.html",
                path="G",
                title="Guide",
                content="quetzalweb guide body",
                sort_order=0,
            )
        ],
        raw_content="quetzalweb guide body",
    )


def _client(conn, monkeypatch, tmp_path, **overrides):
    settings = make_settings(monkeypatch, tmp_path, **overrides)
    app = build_web_app(conn, settings)
    from fastapi.testclient import TestClient

    return TestClient(app)


def test_health_and_search_and_read(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    conn = _open(tmp_path)
    try:
        _seed(conn)
        client = _client(conn, monkeypatch, tmp_path)
        assert client.get("/health").json()["ok"] is True
        res = client.get("/search", params={"query": "quetzalweb"})
        assert res.status_code == 200
        assert res.json()["count"] == 1
        precise = client.get(
            "/search", params={"query": "quetzalweb", "mode": "precise"}
        )
        assert precise.status_code == 200
        assert precise.json()["count"] == 1
        catalog = client.get("/catalog")
        assert catalog.status_code == 200
        assert catalog.json()["count"] == 1
        page = client.get("/read", params={"url": "https://d.example/w.html"})
        assert page.status_code == 200
        assert "quetzalweb" in page.json()["content"]
        assert page.json()["chunk_details"][0]["source_group_id"] == ""
        window_missing = client.get(
            "/read",
            params={"url": "https://d.example/w.html", "mode": "window"},
        )
        assert window_missing.status_code == 400
        window = client.get(
            "/read",
            params={
                "url": "https://d.example/w.html",
                "mode": "window",
                "sort_order": 0,
            },
        )
        assert window.status_code == 200
        usage = client.get("/api-insight", params={"view": "usage"})
        assert usage.status_code == 200
        tree = client.get("/pattern-insight", params={"view": "tree"})
        assert tree.status_code == 200
        jobs = client.get("/jobs")
        assert jobs.status_code == 200
    finally:
        conn.close()


def test_review_flow(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    conn = _open(tmp_path, "review.db")
    try:
        conn.execute("INSERT INTO libraries(id, name) VALUES(1, 'docs')")
        conn.execute(
            "INSERT INTO versions(id, library_id, version) VALUES(1, 1, '1.0')"
        )
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
        client = _client(conn, monkeypatch, tmp_path)
        pending = client.get("/review", params={"action": "list"}).json()
        assert pending["count"] == 1
        assert client.post(
            "/review", json={"entity_id": call_id, "action": "approve"}
        ).json() == {"ok": True}
        assert client.get("/review", params={"action": "list"}).json()["count"] == 0
    finally:
        conn.close()


def test_probe_routes(tmp_path, monkeypatch) -> None:
    import contextlib

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(("llms.txt", "llms-full.txt")):
            return httpx.Response(404)
        if path == "/robots.txt":
            return httpx.Response(200, text="")
        if path == "/sitemap.xml":
            host = str(request.url.host)
            return httpx.Response(
                200,
                text=(
                    '<?xml version="1.0"?><urlset>'
                    f"<url><loc>https://{host}/guide/a</loc></url>"
                    f"<url><loc>https://{host}/blog/b</loc></url></urlset>"
                ),
            )
        if path == "/guide/a":
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text="<html><head><title>Guide A</title></head><body>hi</body></html>",
            )
        if path == "/blog/b":
            return httpx.Response(
                200,
                headers={"content-type": "text/markdown"},
                text="# Beta\n\nbody\n",
            )
        raise AssertionError(path)

    client_transport = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )

    @contextlib.asynccontextmanager
    async def fake(_settings):
        yield client_transport

    import libref.crawler.http as http_mod

    monkeypatch.setattr(http_mod, "build_async_client", fake)
    monkeypatch.chdir(tmp_path)
    conn = _open(tmp_path, "probe.db")
    try:
        client = _client(conn, monkeypatch, tmp_path)
        res = client.post(
            "/doc-probe", json={"start_url": "https://d.example/sitemap.xml"}
        )
        assert res.status_code == 200
        body = res.json()
        assert body["accepted"] == 2
        assert body["budget_exceeded"] is False
        assert len(body["sample_details"]) == 2
        over = client.post(
            "/doc-probe",
            json={"start_url": "https://d.example/sitemap.xml", "max_pages": 1},
        )
        assert over.status_code == 200
        assert over.json()["budget_exceeded"] is True
        bad = client.post("/doc-probe", json={"start_url": ""})
        assert bad.status_code == 422
        repo = tmp_path / "web_repo"
        repo.mkdir()
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "note.txt").write_text("hi\n", encoding="utf-8")
        scoped = client.post(
            "/repo-probe", json={"source": str(repo), "only_files": ["a.py"]}
        )
        assert scoped.status_code == 200
        assert scoped.json()["effective_files"] == 1
        assert scoped.json()["unmatched_only_files"] == []
        assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM repos").fetchone()[0] == 0
    finally:
        conn.close()


def test_unauthorized_when_token_set(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    conn = _open(tmp_path, "auth.db")
    try:
        _seed(conn)
        client = _client(conn, monkeypatch, tmp_path, mcp_token="tok-secret")
        assert client.get("/catalog").status_code == 401
        assert (
            client.get(
                "/catalog", headers={"Authorization": "Bearer tok-secret"}
            ).status_code
            == 200
        )
    finally:
        conn.close()
