"""Client transport maps one HTTP surface into one output contract."""

from __future__ import annotations

from argparse import Namespace

import httpx
import pytest
from librefctl import app
from librefctl.commands import BY_NAME
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
