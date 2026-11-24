"""Identity primitives, scope policy and separation-of-duty rules."""

from __future__ import annotations

import contextvars
import json
from dataclasses import dataclass, field
from typing import TypedDict

from libref.services.infra.tenant import GLOBAL_TENANT, owner_tenant

PLATFORM_ROLE = "platform_admin"
PLATFORM_SCOPE = "platform:admin"

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "external": frozenset({"read", "index:request"}),
    "developer": frozenset(
        {"read", "feedback:write", "feedback:read:own", "index:request"}
    ),
    "kb_admin": frozenset(
        {
            "read",
            "review:read",
            "review:write",
            "feedback:read",
            "feedback:resolve",
            "index:request",
            "index:approve",
        }
    ),
    "sys_admin": frozenset(
        {
            "read",
            "ingest:write",
            "jobs:write",
            "admin:keys",
            "admin:config",
            "index:request",
            "index:approve",
        }
    ),
    PLATFORM_ROLE: frozenset(
        {
            "read",
            "feedback:write",
            "review:read",
            "review:write",
            "feedback:read",
            "feedback:resolve",
            "index:request",
            "index:approve",
            "ingest:write",
            "jobs:write",
            "admin:keys",
            "admin:config",
            PLATFORM_SCOPE,
        }
    ),
}

KNOWN_SCOPES: frozenset[str] = frozenset().union(*ROLE_SCOPES.values())
READ_SCOPES = frozenset({"read"})

# Separation of duties: one principal may hold at most one role from each group.
# The three tenant roles form the submit -> review -> operate chain, so any pair
# collapses two halves of one decision into one person (self-review, or an
# operator approving their own collection job). ``external`` is a read-only
# consumer and conflicts with nothing; ``platform_admin`` is a platform-wide
# role rather than a tenant function, and exempting it is what keeps bootstrap
# usable -- it necessarily holds every scope.
ROLE_SEPARATION_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"developer", "kb_admin", "sys_admin"}),
)
ROLE_SEPARATION_EXEMPT: frozenset[str] = frozenset({PLATFORM_ROLE})


def role_separation_violations(roles: list[str]) -> list[list[str]]:
    """Return the conflicting roles inside each group a principal over-subscribes.

    Each entry lists the roles of one group that must not coexist; an empty list
    means the combination is permitted.
    """
    held = {str(role) for role in (roles or [])}
    if held & ROLE_SEPARATION_EXEMPT:
        return []
    conflicts: list[list[str]] = []
    for group in ROLE_SEPARATION_GROUPS:
        shared = sorted(held & group)
        if len(shared) > 1:
            conflicts.append(shared)
    return conflicts


def check_role_separation(roles: list[str]) -> None:
    """Reject role combinations that break separation of duties."""
    conflicts = role_separation_violations(roles)
    if not conflicts:
        return
    details = "; ".join("+".join(roles_) for roles_ in conflicts)
    raise ValueError(f"roles break separation of duties: {details}")


class RoleSplitPlan(TypedDict):
    """One way to spread a conflicting role set over two principals."""

    keep: str
    move: list[str]
    kept_scopes: list[str]
    move_scopes: list[str]


class AuthDenied(Exception):
    """Authentication or authorization failure with a stable HTTP code."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


@dataclass
class Principal:
    """Authenticated caller with tenant, kind, roles, and effective scopes."""

    id: int | None = None
    tenant_id: str = "default"
    kind: str = "human"
    name: str = ""
    roles: list[str] = field(default_factory=list)
    scopes: set[str] = field(default_factory=set)
    status: str = "active"

    def has(self, scope: str) -> bool:
        return scope in self.scopes

    def can_administer(self, tenant_id: str) -> bool:
        """Whether this caller may manage identities for the target tenant."""
        if self.has(PLATFORM_SCOPE):
            return True
        return tenant_id == self.tenant_id and tenant_id != GLOBAL_TENANT


def missing_scopes(principal: Principal | None, *scopes: str) -> list[str]:
    """List scopes a principal lacks; an absent principal lacks everything."""
    held = set(getattr(principal, "scopes", set()) or ())
    return [scope for scope in scopes if scope not in held]


def require_scopes(principal: Principal | None, *scopes: str) -> None:
    """Reject callers that lack any required scope."""
    missing = missing_scopes(principal, *scopes)
    if missing:
        raise AuthDenied(
            403, "missing_scope", f"forbidden: missing {','.join(missing)}"
        )


def require_any_scope(principal: Principal | None, *scopes: str) -> str:
    """Return the first held scope among alternatives; reject when none is held."""
    held = set(getattr(principal, "scopes", set()) or ())
    for scope in scopes:
        if scope in held:
            return scope
    raise AuthDenied(
        403, "missing_scope", f"forbidden: missing one of {','.join(scopes)}"
    )


def write_tenant(principal: Principal | None) -> str:
    """Resolve the concrete tenant for rows written by a principal."""
    return owner_tenant(getattr(principal, "tenant_id", None))


def _system_caller() -> Principal:
    return Principal(
        tenant_id=GLOBAL_TENANT,
        kind="system",
        name="bootstrap",
        roles=[PLATFORM_ROLE],
        scopes=set(ROLE_SCOPES[PLATFORM_ROLE]),
    )


def system_caller() -> Principal:
    """Platform-level caller for trusted administration on the server host."""
    return _system_caller()


def scopes_for_roles(roles: list[str]) -> set[str]:
    """Expand role names to their granted scopes; unknown roles grant nothing."""
    out: set[str] = set()
    for role in roles or []:
        out |= set(ROLE_SCOPES.get(str(role), frozenset()))
    return out


def _parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


current_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "libref_current_principal", default=None
)
