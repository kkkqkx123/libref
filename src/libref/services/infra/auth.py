"""Request identity for multi-user service.

Principals carry roles, API keys carry hashes only, and effective scopes are
the union of role scopes with key scopes. Secrets never round-trip. The module
owns the single authentication path used by every HTTP exit.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import ipaddress
import json
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from libref.db.contract import MetadataSession
from libref.services.infra.tenant import GLOBAL_TENANT, owner_tenant

PLATFORM_ROLE = "platform_admin"
PLATFORM_SCOPE = "platform:admin"

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "external": frozenset({"read"}),
    "developer": frozenset({"read", "feedback:write"}),
    "kb_admin": frozenset(
        {"read", "review:read", "review:write", "feedback:read", "feedback:resolve"}
    ),
    "sys_admin": frozenset(
        {"read", "ingest:write", "jobs:write", "admin:keys", "admin:config"}
    ),
    PLATFORM_ROLE: frozenset(
        {
            "read",
            "feedback:write",
            "review:read",
            "review:write",
            "feedback:read",
            "feedback:resolve",
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


def scopes_for_roles(roles: list[str]) -> set[str]:
    """Expand role names to their granted scopes; unknown roles grant nothing."""
    out: set[str] = set()
    for role in roles or []:
        out |= set(ROLE_SCOPES.get(str(role), frozenset()))
    return out


def hash_key(raw: str) -> str:
    """Hash a bearer key for storage; the raw value never persists."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_hash(raw: str, stored: str) -> bool:
    """Compare a presented key against its hash in constant time."""
    candidate = hash_key(raw)
    return hmac.compare_digest(candidate, stored)


def verify_key(raw: str | None, stored: str | None) -> bool:
    """Compare a presented key against its stored hash, rejecting empty values."""
    if not raw or not stored:
        return False
    return verify_hash(raw, stored)


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


def _validate_roles(roles: list[str]) -> list[str]:
    unknown = sorted({str(r) for r in (roles or []) if str(r) not in ROLE_SCOPES})
    if unknown:
        raise ValueError(f"unknown roles: {','.join(unknown)}")
    return [str(r) for r in (roles or [])]


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


def _principal_row(conn: MetadataSession, principal_id: int) -> tuple | None:
    return conn.execute(
        "SELECT id, tenant_id, kind, name, roles, status FROM principals WHERE id=?",
        (principal_id,),
    ).fetchone()


def validate_key_scopes(
    scopes: list[str] | None, caller: Principal, *, target_roles: list[str]
) -> list[str]:
    """Require known scopes held by the caller and granted by target roles."""
    requested = sorted({str(s) for s in (scopes or [])})
    unknown = [s for s in requested if s not in KNOWN_SCOPES]
    if unknown:
        raise ValueError(f"unknown scopes: {','.join(unknown)}")
    role_scopes = scopes_for_roles(target_roles)
    caller_scopes = set(caller.scopes)
    excess = [s for s in requested if s not in caller_scopes or s not in role_scopes]
    if excess:
        raise ValueError(f"caller cannot grant scopes: {','.join(excess)}")
    return requested


def _expiry(settings, expires_at: float | None) -> float:
    from libref.config import setting_or_default

    default_hours = float(setting_or_default(settings, "api_key_default_ttl_hours"))
    max_hours = float(setting_or_default(settings, "api_key_max_ttl_hours"))
    now = time.time()
    deadline = now + max_hours * 3600.0
    value = now + default_hours * 3600.0 if expires_at is None else float(expires_at)
    if not value <= deadline:
        raise ValueError("expires_at exceeds the maximum key lifetime")
    return value


def _insert_key(
    conn: MetadataSession,
    *,
    caller: Principal,
    settings,
    principal_id: int,
    raw: str,
    scopes: list[str],
    expires_at: float | None,
) -> int:
    value = _expiry(settings, expires_at)
    cur = conn.execute(
        "INSERT INTO api_keys(principal_id, key_hash, key_prefix, scopes, expires_at, created_by)"
        " VALUES(?,?,?,?,?,?)",
        (
            principal_id,
            hash_key(raw),
            raw[:12],
            json.dumps(scopes, ensure_ascii=False),
            value,
            caller.id,
        ),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def issue_api_key(
    conn: MetadataSession,
    *,
    caller: Principal,
    settings,
    principal_id: int,
    scopes: list[str] | None = None,
    expires_at: float | None = None,
) -> tuple[int, str]:
    """Mint a key bounded by the caller's scopes; only the raw value is usable."""
    ensure_auth_tables(conn)
    target = _principal_row(conn, principal_id)
    if target is None:
        raise ValueError(f"unknown principal: {principal_id}")
    if not caller.can_administer(str(target[1])):
        raise ValueError("caller cannot administer target principal")
    checked_scopes = validate_key_scopes(
        scopes, caller, target_roles=_parse_json_list(str(target[4]))
    )
    raw = f"libref_{secrets.token_hex(24)}"
    key_id = _insert_key(
        conn,
        caller=caller,
        settings=settings,
        principal_id=principal_id,
        raw=raw,
        scopes=checked_scopes,
        expires_at=expires_at,
    )
    return key_id, raw


def revoke_api_key(conn: MetadataSession, *, caller: Principal, key_id: int) -> None:
    """Mark a key revoked; the key must belong to a tenant the caller owns."""
    ensure_auth_tables(conn)
    row = conn.execute(
        "SELECT p.tenant_id FROM api_keys k JOIN principals p ON p.id=k.principal_id"
        " WHERE k.id=?",
        (key_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown api key: {key_id}")
    if not caller.can_administer(str(row[0])):
        raise ValueError("caller cannot administer target key")
    cur = conn.execute(
        "UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
        (time.time(), key_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError(f"unknown api key: {key_id}")


def revoke_principal_keys(
    conn: MetadataSession, *, caller: Principal, principal_id: int
) -> int:
    """Revoke every active key of one principal and return the affected count."""
    ensure_auth_tables(conn)
    row = _principal_row(conn, principal_id)
    if row is None:
        raise ValueError(f"unknown principal: {principal_id}")
    if not caller.can_administer(str(row[1])):
        raise ValueError("caller cannot administer target principal")
    cur = conn.execute(
        "UPDATE api_keys SET revoked_at=? WHERE principal_id=? AND revoked_at IS NULL",
        (time.time(), principal_id),
    )
    conn.commit()
    return int(cur.rowcount)


def lookup_principal(
    conn: MetadataSession, raw_key: str, *, now: float | None = None
) -> Principal | None:
    """Resolve a bearer key to its principal; expired or revoked keys miss."""
    moment = time.time() if now is None else float(now)
    prefix = raw_key[:12] if len(raw_key) >= 12 else raw_key
    rows = conn.execute(
        "SELECT id, principal_id, key_hash, scopes, expires_at, revoked_at, last_used_at"
        " FROM api_keys WHERE key_prefix=?",
        (prefix,),
    ).fetchall()
    for (
        key_id,
        principal_id,
        key_hash,
        scopes_raw,
        expires_at,
        revoked_at,
        last_used,
    ) in rows:
        if revoked_at is not None:
            continue
        if expires_at is not None and float(expires_at) <= moment:
            continue
        if not verify_hash(raw_key, str(key_hash)):
            continue
        prow = _principal_row(conn, int(principal_id))
        if prow is None:
            continue
        principal = _principal_from_row(prow)
        if principal.status != "active":
            continue
        key_scopes = set(_parse_json_list(scopes_raw))
        if key_scopes:
            principal.scopes &= key_scopes
        if last_used is None or moment - float(last_used) > 3600.0:
            conn.execute(
                "UPDATE api_keys SET last_used_at=? WHERE id=?", (moment, key_id)
            )
            conn.commit()
        return principal
    return None


def dev_principal(settings) -> Principal:
    """Fixed principal for explicit loopback development without key checks."""
    name = str(getattr(settings, "dev_principal", "dev-operator") or "dev-operator")
    return Principal(
        id=None,
        tenant_id="default",
        kind="human",
        name=name,
        roles=[PLATFORM_ROLE],
        scopes=set(ROLE_SCOPES[PLATFORM_ROLE]),
    )


def bootstrap_admin(conn: MetadataSession, raw_key: str, settings) -> Principal:
    """Idempotently create the first platform administrator from a bootstrap key."""
    ensure_auth_tables(conn)
    existing = lookup_principal(conn, raw_key)
    if existing is not None:
        return existing
    row = conn.execute("SELECT id FROM principals LIMIT 1").fetchone()
    if row is not None:
        raise ValueError("bootstrap key does not match any issued key")
    system = _system_caller()
    principal_id = create_principal(
        conn,
        caller=system,
        tenant_id=GLOBAL_TENANT,
        kind="human",
        name="bootstrap-admin",
        roles=[PLATFORM_ROLE],
    )
    _insert_key(
        conn,
        caller=system,
        settings=settings,
        principal_id=principal_id,
        raw=raw_key,
        scopes=[],
        expires_at=None,
    )
    found = lookup_principal(conn, raw_key)
    assert found is not None
    return found


current_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "libref_current_principal", default=None
)
