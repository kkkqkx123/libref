"""Server CLI exit boundary: failures flow through the shared error value."""

from __future__ import annotations

import argparse
import json

import pytest

from libref.cli import ops_commands
from libref.cli.common import as_error, report_failure
from libref.cli.parsers import build_parser
from libref.errors import (
    exit_code_for,
    invalid_argument,
    unavailable,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("invalid_argument", 2),
        ("config_refresh_failed", 2),
        ("unavailable", 1),
        ("not_found", 1),
    ],
)
def test_failure_codes_map_to_the_three_tiers(code: str, expected: int) -> None:
    assert exit_code_for(code) == expected


def test_report_failure_renders_text_and_json(capsys) -> None:
    error = invalid_argument("limit must be positive", field="limit")
    code = report_failure(error, argparse.Namespace(json=True))
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.startswith("invalid_argument:")
    payload = json.loads(captured.out.strip())
    assert payload == {
        "error": "invalid_argument",
        "message": "limit must be positive",
        "field": "limit",
    }


def test_unknown_exception_is_not_swallowed() -> None:
    with pytest.raises(RuntimeError):
        as_error(RuntimeError("boom"))


def test_server_parser_keeps_only_local_operations() -> None:
    parser = build_parser()
    commands = parser._subparsers._group_actions[0].choices
    assert {"serve", "serve-mcp", "init-db", "role-audit", "fts-check"} <= set(commands)
    local = commands["local"]._subparsers._group_actions[0].choices
    assert {
        "index-repo",
        "probe-repo",
        "mine",
        "induce-labels",
        "doc-features",
    } <= set(local)
    assert "probe-docs" not in local
    assert not {"search", "answer", "ingest", "request", "feedback"} & set(commands)


def test_unavailable_factory_keeps_outage_on_tier_one() -> None:
    error = unavailable("remote poll timeout")
    assert error.code == "unavailable"
    assert exit_code_for(error.code) == 1


def test_stdio_commands_share_one_assembly(monkeypatch) -> None:
    seen: list[argparse.Namespace] = []
    monkeypatch.setattr(
        ops_commands, "_serve_stdio", lambda args: seen.append(args) or 0
    )
    args = argparse.Namespace(stdio=True)
    assert ops_commands.cmd_serve(args) == 0
    assert ops_commands.cmd_serve_mcp(args) == 0
    assert seen == [args, args]
