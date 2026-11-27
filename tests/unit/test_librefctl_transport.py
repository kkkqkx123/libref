"""Client transport maps one HTTP surface into one output contract."""

from __future__ import annotations

import json
from argparse import Namespace
from typing import Any

import httpx
import pytest

from libref.services.graph_query import GRAPH_DIRECTIONS, GRAPH_MODES
from librefctl import app
from librefctl.app import _collect, _request_body, build_parser
from librefctl.commands import _GRAPH_DIRECTION, _GRAPH_MODE, BY_NAME
from librefctl.output import exit_code, failure, success
from librefctl.transport import Client, ClientError

pytestmark = pytest.mark.unit


def _client(handler) -> Client:
    return Client(
        "http://server/", api_key="secret", transport=httpx.MockTransport(handler)
    )


def test_success_keeps_server_payload_and_bearer_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("Authorization", "")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"items": [], "count": 0})

    client = _client(handler)
    try:
        data = client.call("GET", "/search", params={"query": "torch"})
    finally:
        client.close()
    assert data == {"items": [], "count": 0}
    assert seen["authorization"] == "Bearer secret"
    assert "query=torch" in seen["url"]


def test_server_error_envelope_survives() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "invalid_limit",
                "detail": "limit must be between 1 and 100",
                "request_id": "req-1",
            },
        )

    client = _client(handler)
    try:
        with pytest.raises(ClientError) as raised:
            client.call("GET", "/search")
    finally:
        client.close()
    error = raised.value
    assert error.code == "invalid_limit"
    assert error.message == "limit must be between 1 and 100"
    assert error.request_id == "req-1"
    assert failure(error) == {
        "ok": False,
        "error": {
            "code": "invalid_limit",
            "message": "limit must be between 1 and 100",
            "request_id": "req-1",
        },
    }
    assert exit_code(error.code) == 2


def test_validation_error_maps_to_invalid_argument() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "detail": [
                    {
                        "loc": ["query", "limit"],
                        "msg": "Input should be greater than or equal to 1",
                        "type": "greater_than_equal",
                    }
                ]
            },
        )

    client = _client(handler)
    try:
        with pytest.raises(ClientError) as raised:
            client.call("GET", "/search")
    finally:
        client.close()
    assert raised.value.code == "invalid_argument"
    assert raised.value.field == "limit"


def test_transport_failure_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(handler)
    try:
        with pytest.raises(ClientError) as raised:
            client.call("GET", "/health")
    finally:
        client.close()
    assert raised.value.code == "unavailable"
    assert exit_code(raised.value.code) == 1


def test_success_envelope_shapes_one_object() -> None:
    payload = success({"items": []}, {"waited": True})
    assert payload == {"ok": True, "data": {"items": []}, "meta": {"waited": True}}


def test_wait_polls_until_terminal(monkeypatch) -> None:
    monkeypatch.setattr(app.time, "sleep", lambda _seconds: None)
    states = iter([{"status": "queued"}, {"status": "done"}])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(states))

    client = _client(handler)
    try:
        data, meta = app._wait(
            client, BY_NAME["job wait"], "/jobs/1", Namespace(timeout=10.0)
        )
    finally:
        client.close()
    assert data == {"status": "done"}
    assert meta == {"waited": True, "status": "done", "attempts": 2}


def test_usage_error_is_a_json_envelope(capsys) -> None:
    code = app.main(["search"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"


def test_invalid_timeout_environment_is_a_usage_error(monkeypatch, capsys) -> None:
    monkeypatch.setenv("LIBREF_CLIENT_TIMEOUT", "not-a-number")
    code = app.main(["health"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert "LIBREF_CLIENT_TIMEOUT" in payload["error"]["message"]


def test_non_object_target_filter_is_rejected() -> None:
    command = BY_NAME["request submit"]
    with pytest.raises(ClientError) as raised:
        _request_body(command, {"target_filter": []}, {})
    assert raised.value.field == "target_filter"


def test_dedup_flag_is_tri_state() -> None:
    parser = build_parser()
    unset = parser.parse_args(["pattern-insight", "patterns"])
    enabled = parser.parse_args(["pattern-insight", "patterns", "--dedup"])
    disabled = parser.parse_args(["pattern-insight", "patterns", "--no-dedup"])
    assert (unset.dedup, enabled.dedup, disabled.dedup) == (None, True, False)
    assert _collect(BY_NAME["pattern-insight"], enabled)[1]["dedup"] is True
    assert _collect(BY_NAME["pattern-insight"], disabled)[1]["dedup"] is False


def test_json_object_argument_is_parsed() -> None:
    args = build_parser().parse_args(
        ["doc-probe", "https://d.example/", "--scope", '{"path_prefixes":["/a"]}']
    )
    assert args.scope == {"path_prefixes": ["/a"]}


def test_quota_dict_detail_keeps_readable_information() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "error": "quota_exceeded",
                "detail": {"resource": "jobs", "reset_at": "2026-01-01T00:00:00Z"},
                "request_id": "req-9",
            },
        )

    client = _client(handler)
    try:
        with pytest.raises(ClientError) as raised:
            client.call("POST", "/jobs")
    finally:
        client.close()
    assert "jobs" in raised.value.message
    assert raised.value.request_id == "req-9"


def test_admin_key_issue_maps_repeated_flags_to_lists() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 1, "key": "secret", "prefix": "libref_"})

    args = build_parser().parse_args(
        [
            "admin",
            "key",
            "issue",
            "--name",
            "operator",
            "--role",
            "developer",
            "--role",
            "kb_admin",
            "--scope",
            "read",
        ]
    )
    client = _client(handler)
    try:
        app.dispatch(client, args)
    finally:
        client.close()
    assert seen["body"] == {
        "name": "operator",
        "roles": ["developer", "kb_admin"],
        "scopes": ["read"],
    }


def test_admin_key_attach_uses_principal_path() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": 2, "key": "secret"})

    args = build_parser().parse_args(["admin", "key", "attach", "7", "--scope", "read"])
    client = _client(handler)
    try:
        app.dispatch(client, args)
    finally:
        client.close()
    assert seen["url"] == "http://server/admin/principals/7/keys"


def test_wait_times_out_on_non_terminal_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "queued"})

    client = _client(handler)
    try:
        with pytest.raises(ClientError) as raised:
            app._wait(
                client,
                BY_NAME["request wait"],
                "/index-requests/1",
                Namespace(timeout=0.0),
            )
    finally:
        client.close()
    assert raised.value.code == "unavailable"


def test_graph_command_vocabulary_matches_the_service() -> None:
    assert _GRAPH_MODE == GRAPH_MODES
    assert _GRAPH_DIRECTION == GRAPH_DIRECTIONS
