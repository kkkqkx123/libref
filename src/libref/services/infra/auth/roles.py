"""Separation-of-duty analysis and explicit role conflict splitting."""

from __future__ import annotations

from typing import TypedDict

from libref.db.contract import MetadataSession
from libref.services.infra.auth.identity import (
    PLATFORM_SCOPE,
    ROLE_SCOPES,
    ROLE_SEPARATION_GROUPS,
    Principal,
    _parse_json_list,
    role_separation_violations,
)
from libref.services.infra.auth.keys import issue_api_key
from libref.services.infra.auth.principals import (
    _validate_roles,
    create_principal,
    set_principal_roles,
)
from libref.services.infra.auth.storage import _principal_row, ensure_auth_tables


class RoleSplitPlan(TypedDict):
    """One way to spread a conflicting role set over two principals."""

    keep: str
    move: list[str]
    kept_scopes: list[str]
    move_scopes: list[str]


def separation_advice(roles: list[str]) -> list[RoleSplitPlan]:
    """Enumerate the ways to split a conflicting role set, one plan per kept role.

    The list is advisory: splitting revokes nothing by itself, so an operator
    picks the half that matches who actually does the work and the other half
    moves to a new principal.
    """
    held = {str(role) for role in (roles or [])}
    plans: list[RoleSplitPlan] = []
    for group in ROLE_SEPARATION_GROUPS:
        shared = sorted(held & group)
        if len(shared) <= 1:
            continue
        for keep in shared:
            move = [role for role in shared if role != keep]
            plans.append(
                {
                    "keep": keep,
                    "move": move,
                    "kept_scopes": sorted(ROLE_SCOPES.get(keep, frozenset())),
                    "move_scopes": sorted(
                        frozenset().union(
                            *[ROLE_SCOPES.get(r, frozenset()) for r in move]
                        )
                    ),
                }
            )
    return plans


def pack_roles(roles: list[str]) -> list[list[str]]:
    """Group roles into the fewest sets that each satisfy separation of duties.

    One conflict between two roles does not imply every role needs its own
    principal: a read-only bystander such as ``external`` can share a principal
    with either side, so greedy packing keeps the principal count minimal.
    """
    bins: list[list[str]] = []
    for role in [str(r) for r in (roles or [])]:
        for group in bins:
            if not role_separation_violations([*group, role]):
                group.append(role)
                break
        else:
            bins.append([role])
    return bins


class RoleConflict(TypedDict):
    """One principal whose stored roles span a separation group."""

    principal_id: int
    tenant_id: str
    name: str
    roles: list[str]
    groups: list[list[str]]


def scan_role_conflicts(conn: MetadataSession) -> list[RoleConflict]:
    """List principals already holding a forbidden role combination.

    Existing rows are reported, never rewritten: narrowing a principal silently
    would revoke live access, so remediation stays a human decision.
    """
    rows = conn.execute(
        "SELECT id, tenant_id, name, roles FROM principals ORDER BY id"
    ).fetchall()
    conflicts: list[RoleConflict] = []
    for pid, tenant_id, name, roles_raw in rows:
        roles = _parse_json_list(roles_raw)
        groups = role_separation_violations(roles)
        if not groups:
            continue
        conflicts.append(
            {
                "principal_id": int(pid),
                "tenant_id": str(tenant_id),
                "name": str(name),
                "roles": roles,
                "groups": groups,
            }
        )
    return conflicts


class SplitPart(TypedDict):
    """One replacement principal produced by a split."""

    principal_id: int
    name: str
    roles: list[str]
    key_id: int
    key: str


class SplitResult(TypedDict):
    """Outcome of one explicit role split."""

    dry_run: bool
    principal_id: int
    tenant_id: str
    kept_roles: list[str]
    moved_roles: list[str]
    planned: list[list[str]]
    new_principals: list[SplitPart]


def split_role_conflict(
    conn: MetadataSession,
    *,
    caller: Principal,
    settings,
    principal_id: int,
    keep_role: str,
    new_name: str | None = None,
    dry_run: bool = False,
) -> SplitResult:
    """Move every role but ``keep_role`` onto fresh principals.

    Nothing is rewritten until the caller asks for it: ``dry_run`` returns the
    plan only. When executed, the extra roles are packed into the fewest legal
    principals, each handed a key whose plaintext is returned exactly once.
    Existing keys of the original principal keep working with the narrower role
    set, so the split cannot silently lock anyone out mid-shift.
    """
    ensure_auth_tables(conn)
    row = _principal_row(conn, principal_id)
    if row is None:
        raise ValueError(f"unknown principal: {principal_id}")
    tenant = str(row[1])
    current = _parse_json_list(row[4])
    if not role_separation_violations(current):
        raise ValueError(f"principal {principal_id} holds no conflicting roles")
    keep = str(keep_role or "").strip()
    if keep not in current:
        raise ValueError(f"keep role {keep!r} is not held by principal {principal_id}")
    moved = [role for role in current if role != keep]
    plan = pack_roles(moved)
    base = (new_name or f"{row[3]!s}-split").strip()
    result: SplitResult = {
        "dry_run": bool(dry_run),
        "principal_id": principal_id,
        "tenant_id": tenant,
        "kept_roles": [keep],
        "moved_roles": moved,
        "planned": plan,
        "new_principals": [],
    }
    if dry_run:
        return result
    # Validate the whole plan before writing anything: a rejected pack would
    # otherwise leave a half-split tenant behind.
    for chunk in plan:
        _validate_roles(chunk)
        if not caller.has(PLATFORM_SCOPE):
            excess = [r for r in chunk if r not in caller.roles]
            if excess:
                raise ValueError(f"caller cannot grant roles: {','.join(excess)}")
    for index, chunk in enumerate(plan):
        name = base if index == 0 else f"{base}-{index + 1}"
        new_pid = create_principal(
            conn,
            caller=caller,
            tenant_id=tenant,
            kind=str(row[2]),
            name=name,
            roles=chunk,
        )
        key_id, raw = issue_api_key(
            conn, caller=caller, settings=settings, principal_id=new_pid
        )
        result["new_principals"].append(
            {
                "principal_id": new_pid,
                "name": name,
                "roles": chunk,
                "key_id": key_id,
                "key": raw,
            }
        )
    set_principal_roles(conn, caller=caller, principal_id=principal_id, roles=[keep])
    return result
