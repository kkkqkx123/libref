"""Request authentication for combined serving; routes stay free of business logic."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress


def is_loopback_host(host: str) -> bool:
    """Whether the listen address is a local loopback; failures count as exposed."""
    name = (host or "").strip().lower()
    if name in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def auth_required(host: str, *, auth_mode: str = "enforced") -> bool:
    """Whether per-request authentication applies for this startup mode."""
    return (auth_mode or "enforced") != "dev"


def check_startup(
    host: str,
    *,
    auth_mode: str = "enforced",
    bootstrap_key: str | None = None,
    has_principals: bool = False,
) -> None:
    """Refuse unsafe startups: dev on non-loopback, or enforced with no bootstrap."""
    mode = (auth_mode or "enforced").strip() or "enforced"
    if mode == "dev":
        if not is_loopback_host(host):
            raise ValueError(
                f"refusing dev mode on non-loopback {host}: "
                "use enforced mode with API keys"
            )
        return
    if mode == "enforced":
        if not bootstrap_key and not has_principals:
            raise ValueError(
                "refusing enforced startup with no bootstrap key and no principals: "
                "set LIBREF_BOOTSTRAP_ADMIN_KEY for first boot"
            )
        return
    raise ValueError(f"unknown auth_mode: {mode}")


def parse_bearer(authorization: str | None) -> str | None:
    """Extract a bearer token; malformed headers miss."""
    if not authorization:
        return None
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()


def verify_key(raw: str | None, stored_hash: str | None) -> bool:
    """Compare a presented key against its stored hash in constant time."""
    if not raw or not stored_hash:
        return False
    candidate = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return hmac.compare_digest(candidate, stored_hash)


def is_allowed_origin(origin: str | None, *, host: str) -> bool:
    """Origin check for browser callers; non-browser calls without Origin pass."""
    if not origin:
        return True
    lowered = origin.strip().lower()
    if is_loopback_host(host):
        return "localhost" in lowered or "127.0.0.1" in lowered or "[::1]" in lowered
    return host.lower() in lowered
