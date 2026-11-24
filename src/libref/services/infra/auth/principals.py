"""Principal creation, listing and role assignment."""

from __future__ import annotations

import json
from typing import TypedDict

from libref.db.contract import MetadataSession
from libref.services.infra.auth.identity import (
    PLATFORM_SCOPE,
    ROLE_SCOPES,
    Principal,
    _parse_json_list,
    check_role_separation,
    role_separation_violations,
    scopes_for_roles,
)
from libref.services.infra.auth.storage import _principal_row, ensure_auth_tables


def _validate_roles(roles: list[str]) -> list[str]:
    unknown = sorted({str(r) for r in (roles or []) if str(r) not in ROLE_SCOPES})
    if unknown:
        raise ValueError(f"unknown roles: {','.join(unknown)}")
    checked = [str(r) for r in (roles or [])]
    check_role_separation(checked)
    return checked


def create_principal(
    conn: MetadataSession,
    *,
    caller: Principal,
    tenant_id: str,
    kind: str,
    name: str,
    roles: list[str],
) -> int:
    """Insert a principal row; tenant and roles are constrained by the caller."""
    ensure_auth_tables(conn)
    target = (tenant_id or "").strip()
    if not target:
        raise ValueError("tenant_id is required")
    if not caller.can_administer(target):
        raise ValueError("caller cannot administer target tenant")
    checked_roles = _validate_roles(roles)
    if not caller.has(PLATFORM_SCOPE):
        excess = [r for r in checked_roles if r not in caller.roles]
        if excess:
            raise ValueError(f"caller cannot grant roles: {','.join(excess)}")
    cur = conn.execute(
        "INSERT INTO principals(tenant_id, kind, name, roles) VALUES(?,?,?,?)",
        (target, kind, name, json.dumps(checked_roles, ensure_ascii=False)),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


class PrincipalSummary(TypedDict):
    """Read-only principal row for the admin view."""

    id: int
    tenant_id: str
    kind: str
    name: str
    status: str
    roles: list[str]
    scopes: list[str]
    conflicting: list[list[str]]


def list_principals(
    conn: MetadataSession, *, viewer: Principal, tenant_id: str | None = None
) -> list[PrincipalSummary]:
    """List principals the viewer may administer, flagging stale conflicts.

    Conflicts are reported instead of repaired here: the read path must stay
    side-effect free, and the fix needs a human choice of which role stays.
    """
    wanted = (tenant_id or "").strip()
    sql = "SELECT id, tenant_id, kind, name, roles, status FROM principals"
    params: tuple = ()
    if wanted:
        sql += " WHERE tenant_id=?"
        params = (wanted,)
    sql += " ORDER BY id"
    out: list[PrincipalSummary] = []
    for pid, tenant, kind, name, roles_raw, status in conn.execute(
        sql, params
    ).fetchall():
        if not viewer.can_administer(str(tenant)):
            continue
        roles = _parse_json_list(roles_raw)
        out.append(
            {
                "id": int(pid),
                "tenant_id": str(tenant),
                "kind": str(kind),
                "name": str(name),
                "status": "active" if status is None else str(status),
                "roles": roles,
                "scopes": sorted(scopes_for_roles(roles)),
                "conflicting": role_separation_violations(roles),
            }
        )
    return out


def set_principal_roles(
    conn: MetadataSession, *, caller: Principal, principal_id: int, roles: list[str]
) -> list[str]:
    """Replace a principal's roles under the rules used when creating one."""
    ensure_auth_tables(conn)
    row = _principal_row(conn, principal_id)
    if row is None:
        raise ValueError(f"unknown principal: {principal_id}")
    if not caller.can_administer(str(row[1])):
        raise ValueError("caller cannot administer target principal")
    checked = _validate_roles(roles)
    if not caller.has(PLATFORM_SCOPE):
        excess = [r for r in checked if r not in caller.roles]
        if excess:
            raise ValueError(f"caller cannot grant roles: {','.join(excess)}")
    conn.execute(
        "UPDATE principals SET roles=? WHERE id=?",
        (json.dumps(checked, ensure_ascii=False), principal_id),
    )
    conn.commit()
    return checked
