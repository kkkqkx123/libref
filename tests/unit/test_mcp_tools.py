"""MCP tool visibility: config normalization plus registration filtering."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from libref.config import ConfigError, LibrefSettings, load_settings
from libref.mcp_server.server import ALL_MCP_TOOLS, build_mcp_server


def _isolated_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in [n for n in os.environ if n.startswith("LIBREF_")]:
        monkeypatch.delenv(name, raising=False)


def _tool_names(mcp) -> set[str]:
    return {tool.name for tool in asyncio.run(mcp.list_tools())}


def test_defaults_expose_all_tools(monkeypatch, tmp_path, conn) -> None:
    _isolated_env(monkeypatch, tmp_path)
    settings = LibrefSettings()
    assert settings.mcp_enabled_tools is None
    assert settings.mcp_disabled_tools == []
    assert _tool_names(build_mcp_server(conn, settings)) == set(ALL_MCP_TOOLS)


def test_names_normalize_case_and_blanks(monkeypatch, tmp_path) -> None:
    _isolated_env(monkeypatch, tmp_path)
    conf = tmp_path / "libref.toml"
    conf.write_text(
        '[mcp]\nmcp_enabled_tools = [" Search ", "CATALOG", "search"]\n',
        encoding="utf-8",
    )
    assert load_settings(conf).mcp_enabled_tools == ["search", "catalog"]


def test_env_accepts_json_and_comma_forms(monkeypatch, tmp_path) -> None:
    _isolated_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LIBREF_MCP_ENABLED_TOOLS", '["search", "catalog"]')
    assert LibrefSettings().mcp_enabled_tools == ["search", "catalog"]
    _isolated_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LIBREF_MCP_DISABLED_TOOLS", "web_search, github_search")
    assert LibrefSettings().mcp_disabled_tools == ["web_search", "github_search"]


def test_cli_override_beats_file(monkeypatch, tmp_path) -> None:
    _isolated_env(monkeypatch, tmp_path)
    conf = tmp_path / "libref.toml"
    conf.write_text('[mcp]\nmcp_enabled_tools = ["search"]\n', encoding="utf-8")
    settings = load_settings(conf, {"mcp_enabled_tools": ["catalog"]})
    assert settings.mcp_enabled_tools == ["catalog"]


def test_allowlist_limits_visible_tools(monkeypatch, tmp_path, conn) -> None:
    _isolated_env(monkeypatch, tmp_path)
    settings = LibrefSettings(mcp_enabled_tools=["search", "catalog"])
    assert _tool_names(build_mcp_server(conn, settings)) == {"search", "catalog"}


def test_denylist_removes_conflicting_discovery_tools(
    monkeypatch, tmp_path, conn
) -> None:
    _isolated_env(monkeypatch, tmp_path)
    settings = LibrefSettings(mcp_disabled_tools=["web_search", "github_search"])
    names = _tool_names(build_mcp_server(conn, settings))
    assert names == set(ALL_MCP_TOOLS) - {"web_search", "github_search"}


def test_empty_allowlist_registers_nothing(monkeypatch, tmp_path, conn) -> None:
    _isolated_env(monkeypatch, tmp_path)
    settings = LibrefSettings(mcp_enabled_tools=[])
    assert _tool_names(build_mcp_server(conn, settings)) == set()


def test_unknown_tool_name_fails_fast(monkeypatch, tmp_path, conn) -> None:
    _isolated_env(monkeypatch, tmp_path)
    settings = LibrefSettings(mcp_enabled_tools=["search", "nope"])
    with pytest.raises(ConfigError, match="nope"):
        build_mcp_server(conn, settings)
    denied = LibrefSettings(mcp_disabled_tools=["nope"])
    with pytest.raises(ConfigError, match="nope"):
        build_mcp_server(conn, denied)
