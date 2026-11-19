"""Error contract, page inventory, version flags, and job replay parameters.

These pin the four backend additions the web UI relies on: a stable ``error``
code next to every ``detail``, ``GET /pages`` as the reader entry point, a
``prerelease`` flag on catalog versions, and ``params`` on job details.
"""

from __future__ import annotations

import sqlite3

import pytest

from libref.chunking import Chunk
from libref.db import apply_migrations, open_db
from libref.store.document_store import DocumentStore
from libref.web import build_web_app
from tests.conftest import make_settings

pytestmark = pytest.mark.integration


def _open(tmp_path, name: str = "contract.db") -> sqlite3.Connection:
    conn = open_db(tmp_path / name, check_same_thread=False)
    apply_migrations(conn)
    return conn


def _seed(conn) -> None:
    conn.execute("INSERT INTO libraries(id, name) VALUES(1, 'docs')")
    conn.execute("INSERT INTO versions(id, library_id, version) VALUES(1, 1, '1.0')")
    conn.execute(
        "INSERT INTO versions(id, library_id, version) VALUES(2, 1, '2.0-beta.1')"
    )
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


def _client(conn, monkeypatch, tmp_path):
    return build_web_app(conn, make_settings(monkeypatch, tmp_path, auth_mode="dev"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    conn = _open(tmp_path)
    try:
        _seed(conn)
        from fastapi.testclient import TestClient

        yield TestClient(_client(conn, monkeypatch, tmp_path))
    finally:
        conn.close()


def test_pages_lists_seeded_page(client) -> None:
    res = client.get("/pages", params={"library": "docs", "version": "1.0"})
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["url"] == "https://d.example/w.html"
    assert body["items"][0]["title"] == "Guide"
    assert body["items"][0]["chunks"] == 1


def test_pages_rejects_bad_window(client) -> None:
    body = client.get(
        "/pages", params={"library": "docs", "version": "1.0", "limit": 0}
    ).json()
    assert body["error"] == "invalid_limit"
    assert body["detail"] == "limit must be between 1 and 200"
    body = client.get(
        "/pages", params={"library": "docs", "version": "1.0", "offset": -1}
    ).json()
    assert body["error"] == "invalid_offset"


def test_catalog_flags_prerelease(client) -> None:
    items = client.get("/catalog", params={"library": "docs"}).json()["items"]
    flags = {row["version"]: row["prerelease"] for row in items}
    assert flags == {"1.0": False, "2.0-beta.1": True}


def test_job_detail_exposes_params(client) -> None:
    created = client.post(
        "/jobs",
        json={
            "kind": "mine_patterns",
            "params": {"library": "docs", "version": "1.0"},
        },
    )
    assert created.status_code == 200
    job_id = created.json()["id"]
    detail = client.get(f"/jobs/{job_id}")
    assert detail.status_code == 200
    assert detail.json()["params"]["library"] == "docs"


def test_missing_page_and_job_codes(client) -> None:
    page = client.get("/read", params={"url": "https://d.example/missing.html"})
    assert page.status_code == 404
    body = page.json()
    # request_id is injected by the serving middleware, absent in a bare app.
    assert body["error"] == "page_not_found"
    assert body["detail"] == "page not found"
    job = client.get("/jobs/99999")
    assert job.status_code == 404
    assert job.json()["error"] == "job_not_found"


def test_middleware_rejection_matches_route_shape(tmp_path, monkeypatch) -> None:
    """Middleware guards use the same body builder as route rejections."""
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.routing import Route

    from libref.services.infra.context import build_context
    from libref.web.errors import error_body
    from libref.web.middleware import AuthMiddleware

    monkeypatch.chdir(tmp_path)
    settings = make_settings(monkeypatch, tmp_path, auth_mode="enforced")
    ctx = build_context(settings)
    app = Starlette(routes=[Route("/ping", lambda request: JSONResponse({"ok": True}))])
    app.state.context = ctx
    app.state.settings = settings
    app.add_middleware(AuthMiddleware, context=ctx)
    res = TestClient(app).get("/ping")
    assert res.status_code == 401
    body = res.json()
    assert set(body) == {"error", "detail", "request_id"}
    assert body["error"] == "unauthorized"
    assert body["detail"] == "unauthorized"
    assert body["request_id"]
    # Structured quota details carry their own lead-in text, so the code must be
    # resolved from the payload instead of ``str(dict)``.
    detail = {
        "error": "quota exceeded",
        "resource": "http_requests",
        "reset_at": "2026-11-18T00:00:00Z",
    }
    envelope = error_body(429, detail, "rid", code="quota_exceeded")
    assert envelope["error"] == "quota_exceeded"
    assert envelope["resource"] == "http_requests"
    assert envelope["reset_at"] == "2026-11-18T00:00:00Z"


def test_generic_400_uses_invalid_argument(client) -> None:
    """A 400 without a specific cause still carries a localisable code."""
    res = client.get(
        "/read", params={"url": "https://d.example/w.html", "mode": "window"}
    )
    assert res.status_code == 400
    body = res.json()
    assert body["error"] == "invalid_argument"
    assert body["detail"]


def test_answer_reports_chat_disabled(client) -> None:
    """Chat being unconfigured is a named cause, not a generic argument error."""
    res = client.get("/answer", params={"query": "quetzalweb"})
    assert res.status_code == 400
    body = res.json()
    assert body["error"] == "chat_disabled"
    assert body["detail"]


def test_upstream_failure_uses_named_code(client, monkeypatch) -> None:
    """Registry outages are named, so the UI can say "source unavailable"."""
    from libref.crawler.registry import RegistryError
    from libref.services.registry.discovery import DiscoveryService

    async def boom(*args, **kwargs):
        raise RegistryError("retryable", "registry unreachable")

    monkeypatch.setattr(DiscoveryService, "search_async", boom)
    res = client.get("/library-search", params={"query": "demo"})
    assert res.status_code == 502
    body = res.json()
    assert body["error"] == "upstream_unavailable"
    assert "registry unreachable" in body["detail"]
