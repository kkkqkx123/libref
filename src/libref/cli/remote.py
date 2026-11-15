"""Remote client for server-backed CLI usage; local database stays for maintenance."""

from __future__ import annotations

import argparse
from typing import Any


def is_remote(args: argparse.Namespace, settings) -> bool:
    """Whether this invocation should go through HTTP instead of the local database."""
    if bool(getattr(args, "remote", False)):
        return True
    return False


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
    """One blocking HTTP call with bearer auth; errors surface as messages."""
    import httpx

    headers = auth_headers(settings)
    with httpx.Client(timeout=30.0) as client:
        response = client.request(method, url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("remote server returned a non-object payload")
        return data
