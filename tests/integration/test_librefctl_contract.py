"""Client command table and Web API stay in lockstep."""

from __future__ import annotations

from typing import Any, get_args

import httpx
import pytest
from fastapi.testclient import TestClient

from libref.chunking import Chunk
from libref.db import apply_migrations, open_db
from libref.errors import ALL_CODES, exit_code_for
from libref.services.code.usage_sources import SOURCE_ROLES
from libref.services.ops.index_requests import TERMINAL_STATUSES
from libref.store.document_store import DocumentStore
from libref.web import build_web_app
from libref.web.api import JobStatus
from librefctl.app import _TERMINAL, build_parser, dispatch
from librefctl.commands import _SOURCE_ROLES, BY_NAME, COMMANDS
from librefctl.output import success
from librefctl.transport import USAGE_CODES, Client
from tests.conftest import make_settings

pytestmark = pytest.mark.integration


def _web_client(tmp_path, monkeypatch):
    settings = make_settings(monkeypatch, tmp_path, auth_mode="dev")
    conn = open_db(tmp_path / "ctl.db", check_same_thread=False)
    apply_migrations(conn)
    conn.execute("INSERT INTO libraries(id, name) VALUES(1, 'docs')")
    conn.execute("INSERT INTO versions(id, library_id, version) VALUES(1, 1, '1.0')")
    conn.commit()
    DocumentStore(conn).store_page(
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
    app = build_web_app(conn, settings)
    return conn, TestClient(app)


def _transport(test_client: TestClient) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        response = test_client.request(
            request.method,
            request.url.path,
            params=dict(request.url.params),
            content=request.content,
        )
        return httpx.Response(
            response.status_code, content=response.content, request=request
        )

    return httpx.MockTransport(handler)


def test_retrieval_and_mining_commands_are_differential(tmp_path, monkeypatch) -> None:
    conn, test_client = _web_client(tmp_path, monkeypatch)
    client = Client("http://web", transport=_transport(test_client))
    cases = (
        (["search", "quetzalweb"], {"query": "quetzalweb"}),
        (["pattern-insight", "tree"], {"view": "tree"}),
    )
    try:
        for argv, params in cases:
            args = build_parser().parse_args(argv)
            data, meta = dispatch(client, args)
            command = BY_NAME[args._command.name]
            expected = test_client.get(command.path, params=params).json()
            assert data == expected, command.name
            assert meta == {}
    finally:
        client.close()
        test_client.close()
        conn.close()


def test_every_command_maps_to_a_registered_route(tmp_path, monkeypatch) -> None:
    conn, test_client = _web_client(tmp_path, monkeypatch)
    try:
        routes = {
            (route.path, method)
            for route in test_client.app.routes
            for method in getattr(route, "methods", ())
        }
        for command in COMMANDS:
            assert (command.path, command.method) in routes, command.name
    finally:
        test_client.close()
        conn.close()


def test_dispatch_preserves_web_response(tmp_path, monkeypatch) -> None:
    conn, test_client = _web_client(tmp_path, monkeypatch)
    client = Client("http://web", transport=_transport(test_client))
    try:
        args = build_parser().parse_args(["catalog"])
        data, meta = dispatch(client, args)
        expected = test_client.get("/catalog").json()
        assert data == expected
        assert success(data, meta)["ok"] is True
        assert meta == {}
    finally:
        client.close()
        test_client.close()
        conn.close()


def _resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = spec["components"]["schemas"][name]
    return schema


def _properties(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    schema = _resolve(spec, schema)
    out = dict(schema.get("properties", {}))
    for key in ("allOf", "anyOf", "oneOf"):
        for part in schema.get(key, []):
            out.update(_properties(spec, part))
    return out


def _wrapper_properties(
    spec: dict[str, Any], schema: dict[str, Any], wrapper: str
) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def visit(part: dict[str, Any]) -> None:
        part = _resolve(spec, part)
        wrapped = part.get("properties", {}).get(wrapper)
        if wrapped is not None:
            out.update(_properties(spec, wrapped))
        for key in ("allOf", "anyOf", "oneOf"):
            for child in part.get(key, []):
                visit(child)

    visit(schema)
    return out


def test_client_params_are_valid_web_parameters(tmp_path, monkeypatch) -> None:
    conn, test_client = _web_client(tmp_path, monkeypatch)
    try:
        spec = test_client.app.openapi()
        for command in COMMANDS:
            operation = spec["paths"][command.path][command.method.lower()]
            wire_names = {p["name"] for p in operation.get("parameters", [])}
            for param in command.params:
                if param.location in ("query", "path"):
                    assert param.name in wire_names, (command.name, param.name)

            content = (
                operation.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
            )
            if not content:
                assert not [p for p in command.params if p.location == "body"]
                assert not command.body_extra
                continue
            outer_properties = _properties(spec, content["schema"])
            expected = {p.name for p in command.params if p.location == "body"}
            if command.body_wrapper:
                inner_properties = _wrapper_properties(
                    spec, content["schema"], command.body_wrapper
                )
                assert expected <= set(inner_properties), command.name
                assert set(command.body_extra) <= set(outer_properties), command.name
            else:
                expected |= set(command.body_extra)
                assert expected <= set(outer_properties), command.name
    finally:
        test_client.close()
        conn.close()


def test_usage_codes_match_server_exit_tiers() -> None:
    for code in ALL_CODES:
        assert (code in USAGE_CODES) == (exit_code_for(code) == 2)


def test_wait_states_and_source_roles_match_server_tables() -> None:
    assert _TERMINAL["request"] == TERMINAL_STATUSES
    assert {"done", "failed"} <= _TERMINAL["job"] <= set(get_args(JobStatus))
    assert set(_SOURCE_ROLES) == set(SOURCE_ROLES)
