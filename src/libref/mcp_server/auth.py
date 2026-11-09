"""MCP/HTTP auth: token verification and exposure hardening; routes stay free of business logic."""

from __future__ import annotations

import ipaddress


def is_loopback_host(host: str) -> bool:
    """Whether the listen address is a local loopback; resolution failure is treated as externally exposed."""
    name = (host or "").strip().lower()
    if name in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def auth_required(host: str, *, require_auth: bool, has_token: bool) -> bool:
    """Auth is required when externally exposed or explicitly enabled; loopback without a token is allowed."""
    if require_auth or not is_loopback_host(host):
        return True
    return False


def check_startup(host: str, *, require_auth: bool, token: str | None) -> None:
    """Pre-start hardening check: refuse to start when auth is required but no token is set, never fall back to no auth."""
    if auth_required(host, require_auth=require_auth, has_token=bool(token)):
        if not token:
            raise ValueError(
                f"refusing to expose on {host} without a token: "
                "set LIBREF_MCP_TOKEN or keep the loopback default"
            )


def verify_bearer(authorization: str | None, token: str | None) -> bool:
    """Verify a Bearer token; reject if either side is missing; equal-length comparison avoids timing leaks."""
    if not authorization or not token:
        return False
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential:
        return False
    if len(credential) != len(token):
        return False
    different = 0
    for left, right in zip(credential, token):
        different |= ord(left) ^ ord(right)
    return different == 0


def is_allowed_origin(origin: str | None, *, host: str) -> bool:
    """Origin check: allow when no Origin header (non-browser calls); with a header it must match the listen surface."""
    if not origin:
        return True
    lowered = origin.strip().lower()
    if is_loopback_host(host):
        return "localhost" in lowered or "127.0.0.1" in lowered or "[::1]" in lowered
    return host.lower() in lowered
