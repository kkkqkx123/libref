"""SPA self-hosting tests: shell serving never swallows API or MCP prefixes."""

from __future__ import annotations

import pytest

from libref.web import build_web_app
from tests.conftest import make_settings

pytestmark = pytest.mark.integration


def _spa_dir(tmp_path) -> str:
    root = tmp_path / "dist"
    assets = root / "assets"
    assets.mkdir(parents=True)
    (root / "index.html").write_text("<html>shell</html>", encoding="utf-8")
    (assets / "app.js").write_text("console.log(1)", encoding="utf-8")
    return str(root)


def _client(tmp_path, monkeypatch, **overrides):
    from fastapi.testclient import TestClient

    overrides.setdefault("auth_mode", "dev")
    settings = make_settings(monkeypatch, tmp_path, **overrides)
    app = build_web_app(None, settings)
    return TestClient(app)


def test_shell_and_assets_are_public_but_api_keeps_json(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    dist = _spa_dir(tmp_path)
    client = _client(tmp_path, monkeypatch, auth_mode="enforced", web_spa_dir=dist)
    shell = client.get("/", headers={"Accept": "text/html"})
    assert shell.status_code == 200
    assert "shell" in shell.text
    assert shell.headers["Cache-Control"] == "no-store"
    asset = client.get("/assets/app.js")
    assert asset.status_code == 200
    assert "immutable" in asset.headers["Cache-Control"]
    assert client.get("/catalog").status_code == 401
    missing_json = client.get("/no-such-route")
    assert missing_json.status_code == 401
    missing_html = client.get("/no-such-route", headers={"Accept": "text/html"})
    assert missing_html.status_code == 200
    assert "shell" in missing_html.text
    mcp_html = client.get("/mcp/sse", headers={"Accept": "text/html"})
    assert mcp_html.status_code == 401
    assert "shell" not in mcp_html.text


def test_pure_api_mode_without_spa_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    assert client.get("/health").status_code == 200
    missing = client.get("/no-such-route", headers={"Accept": "text/html"})
    assert missing.status_code == 404
