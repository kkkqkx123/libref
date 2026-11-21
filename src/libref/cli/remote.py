"""Remote client for server-backed CLI usage; local database stays for maintenance."""

from __future__ import annotations

import argparse
from typing import Any


def is_remote(args: argparse.Namespace, settings) -> bool:
    """Whether this invocation should go through HTTP instead of the local database."""
    return bool(getattr(args, "remote", False))


def base_url_for(args: argparse.Namespace, settings) -> str:
    """Resolve the server address from flags, settings, then the loopback default."""
    explicit = getattr(args, "base_url", None)
    if explicit:
        return str(explicit).rstrip("/")
    configured = getattr(settings, "base_url", None)
    if configured:
        return str(configured).rstrip("/")
    host = str(getattr(settings, "web_host", "127.0.0.1") or "127.0.0.1")
    port = int(getattr(settings, "web_port", 8001))
    return f"http://{host}:{port}"


def auth_headers(settings) -> dict[str, str]:
    """Bearer headers from the client key; empty in dev mode without a key."""
    raw = getattr(settings, "client_api_key", None)
    if not raw:
        return {}
    return {"Authorization": f"Bearer {raw}"}


def call_json(
    method: str, url: str, settings, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One blocking HTTP call with bearer auth; the server's failure envelope survives.

    The body is read before the status is judged: the Web exit names the cause in
    ``error``/``detail``/``request_id``, and raising on status alone throws that
    away in favour of ``Client error '400 Bad Request'``.
    """
    import httpx

    from libref.errors import LibrefError

    headers = auth_headers(settings)
    with httpx.Client(timeout=30.0) as client:
        response = client.request(method, url, headers=headers, json=payload)
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code >= 400:
            raise _envelope(response.status_code, data)
        if not isinstance(data, dict):
            raise LibrefError(
                "unavailable",
                "remote server returned a non-object payload",
                hint="check that the base URL points at a libref server",
            )
        return data


def _envelope(status: int, data: Any):
    """Turn a server error body into the shared error value, keeping its request id."""
    from libref.errors import ALL_CODES, LibrefError, coarse, coarse_field

    if isinstance(data, dict) and isinstance(data.get("error"), str):
        detail = data.get("detail")
        message = detail if isinstance(detail, str) else str(detail or "")
        code = data["error"] if data["error"] in ALL_CODES else "unavailable"
        return LibrefError(
            coarse(code),
            message or f"the server rejected the request with status {status}",
            field=coarse_field(code),
            request_id=data.get("request_id") or None,
        )
    return LibrefError("unavailable", f"the server answered with status {status}")
