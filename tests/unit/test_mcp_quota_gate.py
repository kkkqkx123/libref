"""MCP external-call quota gate: denial after limit, pass-through when disabled."""

from __future__ import annotations

import pytest

from libref.mcp_server.context import McpContext
from libref.mcp_server.contract import ToolFailure
from libref.services.infra import quota as quota_mod
from tests.conftest import make_settings


def _ctx(conn, settings) -> McpContext:
    return McpContext(
        conn,
        settings,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )


def test_gate_disabled_when_quota_off(conn, monkeypatch, tmp_path) -> None:
    ctx = _ctx(conn, make_settings(monkeypatch, tmp_path))
    ctx.quota_gate("answer")  # No policy, no gating: must not raise.


def test_gate_denies_after_limit(conn, monkeypatch, tmp_path) -> None:
    settings = make_settings(
        monkeypatch, tmp_path, quota_enabled=True, quota_external_calls_per_hour=1
    )
    quota_mod.invalidate_policy_cache()
    ctx = _ctx(conn, settings)
    ctx.quota_gate("answer")
    with pytest.raises(ToolFailure) as found:
        ctx.quota_gate("web_search")
    assert found.value.code == "quota_exceeded"


def test_gate_passes_with_unlimited_policy(conn, monkeypatch, tmp_path) -> None:
    settings = make_settings(
        monkeypatch, tmp_path, quota_enabled=True, quota_external_calls_per_hour=0
    )
    quota_mod.invalidate_policy_cache()
    ctx = _ctx(conn, settings)
    ctx.quota_gate("resolve")
    ctx.quota_gate("github_search")
