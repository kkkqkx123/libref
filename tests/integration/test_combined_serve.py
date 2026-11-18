"""Combined-serve regression tests: flattened routes keep API, MCP, and shell behavior."""

from __future__ import annotations

import pytest
from fastapi.middleware.asyncexitstack import AsyncExitStackMiddleware
from fastapi.testclient import TestClient
from starlette.applications import Starlette

from libref.db import apply_migrations, open_db
from libref.services.infra.context import build_context
from libref.web import build_web_app
from libref.web.middleware import AuthMiddleware
from libref.web.spa import SpaMiddleware, spa_dir_for
from tests.conftest import make_settings

pytestmark = pytest.mark.integration


def _combined(tmp_path, monkeypatch, **overrides):
    """Replicate the cmd_serve assembly: flattened routes plus combined middlewares."""
    db_path = tmp_path / "combined.db"
    conn = open_db(db_path, check_same_thread=False)
    apply_migrations(conn)
    overrides.setdefault("auth_mode", "dev")
    settings = make_settings(monkeypatch, tmp_path, db_path=str(db_path), **overrides)
    ctx = build_context(settings)
    web_app = build_web_app(conn, settings, context=ctx)
    routes = list(web_app.routes)
    combined = Starlette(routes=routes)
    combined.state.context = ctx
    combined.state.settings = settings
    combined.add_middleware(AuthMiddleware, context=ctx)
    combined.add_middleware(AsyncExitStackMiddleware)
    spa_directory = spa_dir_for(settings)
    if spa_directory is not None:
        combined.add_middleware(
            SpaMiddleware,
            directory=spa_directory,
            mcp_prefix=str(getattr(settings, "mcp_path", "/mcp") or "/mcp"),
            api_paths={
                str(getattr(route, "path", ""))
                for route in routes
                if getattr(route, "path", "")
            },
        )
    return TestClient(combined)


def _spa_dir(tmp_path) -> str:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<html>shell</html>", encoding="utf-8")
    return str(root)


def test_flattened_api_routes_keep_json_contract(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    client = _combined(tmp_path, monkeypatch)
    res = client.get("/catalog")
    assert res.status_code == 200
    assert res.json()["count"] == 0
    me = client.get("/me")
    assert me.status_code == 200
    assert me.json()["name"] == "dev-operator"


def test_flattened_shell_never_swallows_api_or_mcp(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    dist = _spa_dir(tmp_path)
    client = _combined(tmp_path, monkeypatch, web_spa_dir=dist)
    shell = client.get("/", headers={"Accept": "text/html"})
    assert shell.status_code == 200
    assert "shell" in shell.text
    assert client.get("/catalog").json()["count"] == 0
    missing = client.get("/no-such-route", headers={"Accept": "text/html"})
    assert missing.status_code == 200
    assert "shell" in missing.text
    mcp = client.get("/mcp/sse", headers={"Accept": "text/html"})
    assert mcp.status_code == 404
    assert "shell" not in mcp.text
