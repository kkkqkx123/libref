"""Request-level authentication, bearer and origin checks."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from libref.db.contract import MetadataSession
from libref.services.infra.auth.identity import AuthDenied, Principal
from libref.services.infra.auth.keys import dev_principal, lookup_principal


def is_loopback_host(host: str) -> bool:
    """Whether the listen address is a local loopback; failures count as exposed."""
    name = (host or "").strip().lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def parse_bearer(authorization: str | None) -> str | None:
    """Extract a bearer token; malformed headers miss."""
    if not authorization:
        return None
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()


def _origin_key(raw: str) -> tuple[str, str, int]:
    parts = urlsplit(raw.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"invalid origin: {raw}")
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        raise ValueError(f"invalid origin: {raw}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, parts.hostname.lower().rstrip("."), port


def validate_origin_config(origins: list[str] | None) -> None:
    """Reject wildcard or malformed origins before they can weaken request checks."""
    for raw in origins or []:
        text = str(raw).strip()
        if text == "*":
            raise ValueError("allowed_origins must not contain '*'")
        _origin_key(text)


def is_allowed_origin(origin: str | None, allowed_origins: list[str] | None) -> bool:
    """Exact origin matching; requests without an Origin are non-browser calls."""
    if not origin:
        return True
    try:
        presented = _origin_key(origin)
    except ValueError:
        return False
    return any(presented == _origin_key(str(item)) for item in (allowed_origins or []))


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


def authenticate_request(
    conn: MetadataSession,
    settings,
    *,
    authorization: str | None,
    origin: str | None,
) -> Principal:
    """Resolve one request to a principal; the only HTTP authentication entry."""
    mode = str(getattr(settings, "auth_mode", "enforced") or "enforced")
    if mode == "dev":
        return dev_principal(settings)
    allowed = list(getattr(settings, "allowed_origins", None) or [])
    if not is_allowed_origin(origin, allowed):
        raise AuthDenied(403, "origin_forbidden", "origin not allowed")
    raw = parse_bearer(authorization)
    if raw is None:
        raise AuthDenied(401, "unauthorized", "unauthorized")
    principal = lookup_principal(conn, raw)
    if principal is None:
        raise AuthDenied(401, "unauthorized", "unauthorized")
    return principal
