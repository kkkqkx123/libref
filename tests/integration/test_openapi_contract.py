"""OpenAPI contract tests: key routes exist and expose object response schemas."""

from __future__ import annotations

import sqlite3

import pytest

from libref.chunking import Chunk
from libref.db import apply_migrations, open_db
from libref.store.document_store import DocumentStore
from libref.web import build_web_app
from tests.conftest import make_settings

pytestmark = pytest.mark.integration

_CONTRACT_PATHS = ("/search", "/answer", "/read", "/review")


def _open(tmp_path, name: str = "contract.db") -> sqlite3.Connection:
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
        "https://d.example/a.html",
        "stored",
        chunks=[
            Chunk(
                url="https://d.example/a.html",
                path="G",
                title="Guide",
                content="quetzalweb guide body",
                sort_order=0,
            )
        ],
        raw_content="quetzalweb guide body",
    )


def _client(conn, monkeypatch, tmp_path):
    settings = make_settings(monkeypatch, tmp_path)
    app = build_web_app(conn, settings)
    from fastapi.testclient import TestClient

    return TestClient(app)


def test_contract_routes_in_openapi(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    conn = _open(tmp_path)
    try:
        _seed(conn)
        client = _client(conn, monkeypatch, tmp_path)
        spec = client.get("/openapi.json")
        assert spec.status_code == 200
        document = spec.json()
        paths = document["paths"]
        components = document.get("components", {}).get("schemas", {})
        for route in _CONTRACT_PATHS:
            assert route in paths, route
            response = paths[route]["get"]["responses"]["200"]
            schema = response["content"]["application/json"]["schema"]
            ref = schema.get("$ref", "")
            assert ref.startswith("#/components/schemas/"), route
            name = ref.rsplit("/", 1)[-1]
            assert components[name].get("type") == "object", route
            assert components[name].get("properties"), route
    finally:
        conn.close()


def test_contract_search_response_shape(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    conn = _open(tmp_path)
    try:
        _seed(conn)
        client = _client(conn, monkeypatch, tmp_path)
        body = client.get("/search", params={"query": "quetzalweb"}).json()
        for field in (
            "items",
            "degraded",
            "count",
            "reasons",
            "answerable",
            "answerability",
        ):
            assert field in body, field
        page = client.get("/read", params={"url": "https://d.example/a.html"}).json()
        for field in ("url", "content", "chunks", "chunk_details"):
            assert field in page, field
        review = client.get("/review").json()
        for field in ("items", "degraded", "count"):
            assert field in review, field
    finally:
        conn.close()
