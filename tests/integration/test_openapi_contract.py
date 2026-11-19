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

_CONTRACT_PATHS = ("/search", "/answer", "/read", "/pages", "/review")
_METHODS = frozenset({"get", "post", "put", "delete"})


def _schema_refs(schema: dict) -> list[str]:
    if "$ref" in schema:
        return [schema["$ref"]]
    refs: list[str] = []
    for variant in schema.get("anyOf", []):
        refs.extend(_schema_refs(variant))
    return refs


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
    settings = make_settings(monkeypatch, tmp_path, auth_mode="dev")
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


def test_all_routes_expose_named_object_schemas(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    conn = _open(tmp_path)
    try:
        client = _client(conn, monkeypatch, tmp_path)
        document = client.get("/openapi.json").json()
        components = document.get("components", {}).get("schemas", {})
        for route, operations in document["paths"].items():
            for method, operation in operations.items():
                if method not in _METHODS:
                    continue
                response = operation["responses"]["200"]
                schema = response["content"]["application/json"]["schema"]
                refs = _schema_refs(schema)
                assert refs, f"{method.upper()} {route}"
                for ref in refs:
                    assert ref.startswith("#/components/schemas/"), ref
                    name = ref.rsplit("/", 1)[-1]
                    assert components[name].get("type") == "object", name
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


def test_response_contract_is_strict(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    conn = _open(tmp_path)
    try:
        document = _client(conn, monkeypatch, tmp_path).get("/openapi.json").json()
        schemas = document["components"]["schemas"]
        for name, schema in schemas.items():
            assert schema.get("additionalProperties") is not True, name
        for name, field in (
            ("JobDetail", "status"),
            ("JobSummary", "status"),
            ("IndexRequestItem", "status"),
            ("ReviewItem", "status"),
            ("FeedbackItem", "status"),
            ("QuotaPolicy", "window_type"),
        ):
            assert "enum" in schemas[name]["properties"][field], (name, field)
        for field in ("expires_at", "revoked_at", "last_used_at", "created_at"):
            prop = schemas["ApiKeyRow"]["properties"][field]
            types = {variant["type"] for variant in prop.get("anyOf", [prop])}
            assert types == {"string", "null"}, field
        for name, expected in (
            ("LibraryResponse", "libraries"),
            ("VersionResponse", "versions"),
            ("PageLinkStats", "page"),
            ("VersionLinkStats", "version"),
        ):
            assert schemas[name]["properties"]["kind"]["const"] == expected, name
        for operations in document["paths"].values():
            for method, operation in operations.items():
                if method not in _METHODS:
                    continue
                default = operation["responses"]["default"]["content"]
                schema = default["application/json"]["schema"]
                assert schema["$ref"].endswith("/ErrorResponse"), (method, operation)
    finally:
        conn.close()
