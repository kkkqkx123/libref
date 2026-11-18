"""Tenant scoping for shared-library isolation.

Every business row carries a tenant identifier. The default tenant keeps
single-user databases working without explicit principals. The global tenant
bypasses row filtering but forces audit writes.
"""

from __future__ import annotations

from typing import Any

DEFAULT_TENANT = "default"
DEV_TENANT = "dev"
GLOBAL_TENANT = "*"

_EXEMPT_TABLES = frozenset({"robots_cache", "registry_cache", "schema_meta"})


def normalize_tenant(raw: str | None) -> str:
    """Normalize an incoming tenant marker to its canonical form."""
    text = (raw or "").strip()
    if not text:
        return DEFAULT_TENANT
    return text


def is_global(tenant_id: str) -> bool:
    """Whether the tenant bypasses row filtering for platform administration."""
    return tenant_id == GLOBAL_TENANT


def table_has_tenant(conn: Any, table: str) -> bool:
    """Whether the given table carries a tenant column in this database."""
    if table in _EXEMPT_TABLES:
        return False
    if hasattr(conn, "table_has_tenant"):
        return bool(conn.table_has_tenant(table))
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == "tenant_id" for row in rows)


def tenant_clause(
    conn: Any, table: str, tenant_id: str, alias: str | None = None
) -> tuple[str, list[str]]:
    """Build a tenant predicate for one table; empty when exempt or global."""
    if is_global(tenant_id):
        return "", []
    if not table_has_tenant(conn, table):
        return "", []
    prefix = f"{alias}." if alias else ""
    return f"{prefix}tenant_id = ?", [tenant_id]


def require_tenant(tenant_id: str | None) -> str:
    """Reject missing tenant markers instead of silently falling back."""
    text = (tenant_id or "").strip()
    if not text:
        raise ValueError("tenant is required")
    return text


def resolve_version_id(
    conn: Any,
    tenant_id: str,
    library: str,
    version: str,
    ecosystem: str | None = None,
) -> int:
    """Resolve a library version inside one tenant; unknown or foreign rows miss."""
    tenant_id = require_tenant(tenant_id)
    if is_global(tenant_id):
        row = conn.execute(
            "SELECT v.id FROM versions v JOIN libraries l ON l.id = v.library_id"
            " WHERE l.name=? AND v.version=? AND (? IS NULL OR l.ecosystem=?)",
            (library, version, ecosystem, ecosystem),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT v.id FROM versions v JOIN libraries l ON l.id = v.library_id"
            " WHERE l.name=? AND v.version=? AND (? IS NULL OR l.ecosystem=?)"
            " AND l.tenant_id=? AND v.tenant_id=?",
            (library, version, ecosystem, ecosystem, tenant_id, tenant_id),
        ).fetchone()
    if row is None:
        raise ValueError(f"unknown version: {library} {version}")
    return int(row[0])


def library_ids(conn: Any, tenant_id: str) -> list[int] | None:
    """Version-owning library ids visible to one tenant; None means unfiltered."""
    tenant_id = require_tenant(tenant_id)
    if is_global(tenant_id):
        return None
    if not table_has_tenant(conn, "libraries"):
        return None
    rows = conn.execute(
        "SELECT id FROM libraries WHERE tenant_id=?", (tenant_id,)
    ).fetchall()
    return [int(row[0]) for row in rows]


def tenant_snapshot_dir(settings, tenant_id: str | None = None):
    """Per-tenant snapshot root; default tenant keeps the legacy path."""
    from pathlib import Path as _Path

    base = _Path(str(settings.snapshot_dir))
    tenant = normalize_tenant(tenant_id)
    if tenant == DEFAULT_TENANT:
        return base
    safe = (
        "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in tenant)[:64]
        or DEFAULT_TENANT
    )
    target = base / f"tenant-{safe}"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return target


def tenant_repos_dir(settings, tenant_id: str | None = None):
    """Per-tenant repos root; default tenant keeps the legacy path."""
    from pathlib import Path as _Path

    base = _Path(str(settings.repos_dir))
    tenant = normalize_tenant(tenant_id)
    if tenant == DEFAULT_TENANT:
        return base
    safe = (
        "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in tenant)[:64]
        or DEFAULT_TENANT
    )
    target = base / f"tenant-{safe}"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return target


def validate_scrape_target(start_url: str, settings) -> str:
    """Validate a scrape start URL; rejects non-http, loopback and private ranges."""
    import ipaddress as _ip
    from urllib.parse import urlparse as _urlparse

    text = (start_url or "").strip()
    if not text:
        raise ValueError("start_url is empty")
    try:
        parsed = _urlparse(text)
    except Exception as exc:
        raise ValueError(f"invalid start_url: {start_url}") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported scrape scheme: {scheme or '(missing)'}")
    host = (parsed.hostname or "").strip()
    if not host:
        raise ValueError("scrape target host is empty")
    lowered = host.lower()
    # Host allowlist gates external fetching when configured.
    allowed = list(getattr(settings, "allowed_hosts", None) or [])
    if allowed:
        norm_allowed = {
            str(h).strip().lower().rstrip(".") for h in allowed if str(h).strip()
        }
        if lowered.rstrip(".") not in norm_allowed and not any(
            lowered.endswith("." + a) for a in norm_allowed if a
        ):
            raise ValueError(f"scrape host not allowlisted: {host}")
    # Loopback and private ranges are rejected except for dev-local loopback.
    auth_mode = str(getattr(settings, "auth_mode", "enforced") or "enforced")
    is_loopback_name = lowered in (
        "localhost",
        "localhost.",
        "ip6-localhost",
    ) or lowered.endswith(".localhost")
    try:
        addr = _ip.ip_address(lowered.strip("[]"))
        is_loop = addr.is_loopback
        is_priv = (
            addr.is_private
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
        )
    except ValueError:
        is_loop = is_loopback_name
        # Plain hostnames that resolve to local suffixes are treated as private.
        is_priv = is_loopback_name or lowered.endswith(
            (".local", ".internal", ".lan", ".home", ".corp")
        )
    if (is_loop or is_priv) and not (auth_mode == "dev" and is_loop):
        raise ValueError(f"scrape target not allowed: {host}")
    return text
