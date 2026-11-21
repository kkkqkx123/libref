"""MCP failure contract over the wire: one isError signal and one compact payload."""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from tests.conftest import seed_mcp_db

SERVER_BIN = Path(sys.executable).parent / "libref"

pytestmark = [pytest.mark.integration, pytest.mark.requires_binary]

# label, tool, arguments, expected code, expected field, character budget
CALLER_MISTAKES = [
    ("missing-required", "search", {"limit": 3}, "invalid_argument", "query", 160),
    (
        "type-error",
        "search",
        {"query": "torch", "limit": "abc"},
        "invalid_argument",
        "limit",
        160,
    ),
    (
        "boolean-as-integer",
        "search",
        {"query": "torch", "limit": True},
        "invalid_argument",
        "limit",
        160,
    ),
    (
        "fraction-as-integer",
        "search",
        {"query": "torch", "limit": 5.7},
        "invalid_argument",
        "limit",
        160,
    ),
    (
        "unknown-parameter",
        "search",
        {"query": "torch", "top_k": 3},
        "invalid_argument",
        "top_k",
        200,
    ),
    (
        "enum-member",
        "api_insight",
        {"view": "bogus"},
        "invalid_argument",
        "view",
        160,
    ),
    (
        "range",
        "search",
        {"query": "torch", "limit": 0},
        "invalid_argument",
        "limit",
        160,
    ),
]

_FORBIDDEN = ("validation error", "pydantic", "Traceback", "input_value")


@asynccontextmanager
async def _session(db_path: Path, workdir: Path):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=str(SERVER_BIN),
        args=["serve-mcp"],
        env={"LIBREF_DB_PATH": str(db_path)},
        cwd=str(workdir),
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        yield session


def _payload(result) -> dict:
    text = result.content[0].text
    parsed = json.loads(text)
    assert isinstance(parsed, dict), text
    return parsed


def _text(result) -> str:
    return result.content[0].text


def test_caller_mistakes_carry_one_compact_payload(tmp_path, monkeypatch) -> None:
    import asyncio

    db_path = tmp_path / "contract.db"
    seed_mcp_db(db_path, "quetzalcontract", "https://d.example/contract.html")
    monkeypatch.setenv("LIBREF_DB_PATH", str(db_path))

    async def run() -> None:
        async with _session(db_path, tmp_path) as session:
            await session.initialize()
            for label, tool, arguments, code, field, budget in CALLER_MISTAKES:
                result = await session.call_tool(tool, arguments)
                text = _text(result)
                assert result.is_error, f"{label}: caller mistakes are failures"
                assert len(text) <= budget, f"{label}: {len(text)} chars"
                payload = _payload(result)
                assert payload["error"] == code, f"{label}: {payload}"
                assert payload.get("field") == field, f"{label}: {payload}"
                assert payload["message"], label
                lowered = text.lower()
                for banned in _FORBIDDEN:
                    assert banned.lower() not in lowered, f"{label}: {text}"

    asyncio.run(run())


def test_unknown_parameter_names_the_accepted_ones(tmp_path, monkeypatch) -> None:
    import asyncio

    db_path = tmp_path / "accepted.db"
    seed_mcp_db(db_path, "quetzalcontract", "https://d.example/contract.html")
    monkeypatch.setenv("LIBREF_DB_PATH", str(db_path))

    async def run() -> None:
        async with _session(db_path, tmp_path) as session:
            await session.initialize()
            result = await session.call_tool("search", {"query": "x", "top_k": 3})
            hint = _payload(result)["hint"]
            for accepted in ("query", "limit", "preset"):
                assert accepted in hint, hint

    asyncio.run(run())


def test_business_outcomes_stay_results(tmp_path, monkeypatch) -> None:
    """A missing job is an answer, not a failure: callers keep their control flow."""
    import asyncio

    db_path = tmp_path / "business.db"
    seed_mcp_db(db_path, "quetzalcontract", "https://d.example/contract.html")
    monkeypatch.setenv("LIBREF_DB_PATH", str(db_path))

    async def run() -> None:
        async with _session(db_path, tmp_path) as session:
            await session.initialize()
            missing = await session.call_tool("job_show", {"job_id": 987654})
            assert not missing.is_error
            assert _payload(missing)["error"] == "not_found"

            page = await session.call_tool(
                "read_doc", {"url": "https://d.example/absent.html"}
            )
            assert not page.is_error

    asyncio.run(run())


def test_string_numbers_are_still_accepted(tmp_path, monkeypatch) -> None:
    """Clients do send "5" for an integer; rejecting that breaks real callers."""
    import asyncio

    db_path = tmp_path / "coerce.db"
    seed_mcp_db(db_path, "quetzalcontract", "https://d.example/contract.html")
    monkeypatch.setenv("LIBREF_DB_PATH", str(db_path))

    async def run() -> None:
        async with _session(db_path, tmp_path) as session:
            await session.initialize()
            result = await session.call_tool("search", {"query": "x", "limit": "5"})
            assert not result.is_error

    asyncio.run(run())
