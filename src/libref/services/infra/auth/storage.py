"""Shared principal table helpers for identity storage."""

from __future__ import annotations

from libref.db.contract import MetadataSession
from libref.services.infra.auth.identity import (
    Principal,
    _parse_json_list,
    scopes_for_roles,
)


def _principal_from_row(row: tuple) -> Principal:
    roles = _parse_json_list(row[4])
    return Principal(
        id=int(row[0]),
        tenant_id=str(row[1]),
        kind=str(row[2]),
        name=str(row[3]),
        roles=roles,
        scopes=scopes_for_roles(roles),
        status=str(row[5]) if len(row) > 5 else "active",
    )


def ensure_auth_tables(conn: MetadataSession) -> None:
    """Create identity tables when absent; idempotent for fresh test databases."""
    from libref.db.schema import AUTH_TABLES_SQL

    conn.executescript(AUTH_TABLES_SQL)
    conn.commit()


def _principal_row(conn: MetadataSession, principal_id: int) -> tuple | None:
    return conn.execute(
        "SELECT id, tenant_id, kind, name, roles, status FROM principals WHERE id=?",
        (principal_id,),
    ).fetchone()


def principal_tenant(conn: MetadataSession, principal_id: int) -> str | None:
    """Tenant owning one principal; ``None`` when the id is unknown."""
    row = _principal_row(conn, int(principal_id))
    return None if row is None else str(row[1])
