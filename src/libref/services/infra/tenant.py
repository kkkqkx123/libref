"""Tenant scoping for shared-library isolation.

Every business row carries a tenant identifier. The default tenant keeps
single-user databases working without explicit principals. The global tenant
bypasses row filtering but forces audit writes.
"""

from __future__ import annotations

import sqlite3

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


def table_has_tenant(conn: sqlite3.Connection, table: str) -> bool:
    """Whether the given table carries a tenant column in this database."""
    if table in _EXEMPT_TABLES:
        return False
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == "tenant_id" for row in rows)


def tenant_clause(
    conn: sqlite3.Connection, table: str, tenant_id: str, alias: str | None = None
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
    conn: sqlite3.Connection,
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


def library_ids(conn: sqlite3.Connection, tenant_id: str) -> list[int] | None:
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
