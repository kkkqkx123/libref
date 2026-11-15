"""Request identity for multi-user service.

Principals carry roles, API keys carry hashes only, and effective scopes are
the union of role scopes with key scopes. Secrets never round-trip.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass, field

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "developer": frozenset({"read", "feedback:write"}),
    "kb_admin": frozenset(
        {"read", "review:read", "review:write", "feedback:read", "feedback:resolve"}
    ),
    "sys_admin": frozenset(
        {"read", "ingest:write", "jobs:write", "admin:keys", "admin:config"}
    ),
    "external": frozenset({"read"}),
}

READ_SCOPES = frozenset({"read"})


@dataclass
class Principal:
    """Authenticated caller with tenant, kind, roles, and effective scopes."""

    id: int | None = None
    tenant_id: str = "default"
    kind: str = "human"
    name: str = ""
    roles: list[str] = field(default_factory=list)
    scopes: set[str] = field(default_factory=set)


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


def _principal_from_row(row: sqlite3.Row | tuple) -> Principal:
    return Principal(
        id=int(row[0]),
        tenant_id=str(row[1]),
        kind=str(row[2]),
        name=str(row[3]),
        roles=_parse_json_list(row[4]),
        scopes=scopes_for_roles(_parse_json_list(row[4])),
    )


def ensure_auth_tables(conn: sqlite3.Connection) -> None:
    """Create identity tables when absent; idempotent for fresh test databases."""
    from libref.db.schema import AUTH_TABLES_SQL

    conn.executescript(AUTH_TABLES_SQL)
    conn.commit()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def create_principal(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    kind: str,
    name: str,
    roles: list[str],
) -> int:
    """Insert a principal row and return its id."""
    ensure_auth_tables(conn)
    cur = conn.execute(
        "INSERT INTO principals(tenant_id, kind, name, roles) VALUES(?,?,?,?)",
        (tenant_id, kind, name, json.dumps(list(roles), ensure_ascii=False)),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def issue_api_key(
    conn: sqlite3.Connection,
    *,
    principal_id: int,
    scopes: list[str] | None = None,
    created_by: int | None = None,
) -> tuple[int, str]:
    """Mint a key for one principal; only the returned raw value is usable."""
    ensure_auth_tables(conn)
    raw = f"libref_{secrets.token_hex(24)}"
    prefix = raw[:12]
    cur = conn.execute(
        "INSERT INTO api_keys(principal_id, key_hash, key_prefix, scopes, created_by)"
        " VALUES(?,?,?,?,?)",
        (
            principal_id,
            hash_key(raw),
            prefix,
            json.dumps(list(scopes or []), ensure_ascii=False),
            created_by,
        ),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid), raw


def revoke_api_key(conn: sqlite3.Connection, key_id: int) -> None:
    """Mark a key revoked; revoked keys stop authenticating immediately."""
    ensure_auth_tables(conn)
    cur = conn.execute(
        "UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
        (time.time(), key_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError(f"unknown api key: {key_id}")


def lookup_principal(
    conn: sqlite3.Connection, raw_key: str, *, now: float | None = None
) -> Principal | None:
    """Resolve a bearer key to its principal; expired or revoked keys miss."""
    ensure_auth_tables(conn)
    moment = time.time() if now is None else float(now)
    prefix = raw_key[:12] if len(raw_key) >= 12 else raw_key
    rows = conn.execute(
        "SELECT id, principal_id, key_hash, scopes, expires_at, revoked_at"
        " FROM api_keys WHERE key_prefix=?",
        (prefix,),
    ).fetchall()
    for key_id, principal_id, key_hash, scopes_raw, expires_at, revoked_at in rows:
        if revoked_at is not None:
            continue
        if expires_at is not None and float(expires_at) <= moment:
            continue
        if not verify_hash(raw_key, str(key_hash)):
            continue
        prow = conn.execute(
            "SELECT id, tenant_id, kind, name, roles FROM principals WHERE id=?",
            (principal_id,),
        ).fetchone()
        if prow is None:
            continue
        principal = _principal_from_row(prow)
        key_scopes = set(_parse_json_list(scopes_raw))
        principal.scopes |= key_scopes
        return principal
    return None


def dev_principal(settings) -> Principal:
    """Fixed principal for loopback development without key checks."""
    name = str(getattr(settings, "dev_principal", "dev-operator") or "dev-operator")
    return Principal(
        id=None,
        tenant_id="default",
        kind="human",
        name=name,
        roles=["sys_admin", "kb_admin", "developer"],
        scopes=set().union(
            scopes_for_roles(["sys_admin", "kb_admin", "developer"])
        ),
    )


def bootstrap_admin(conn: sqlite3.Connection, raw_key: str) -> Principal:
    """Idempotently create the first platform administrator from a bootstrap key."""
    ensure_auth_tables(conn)
    existing = lookup_principal(conn, raw_key)
    if existing is not None:
        return existing
    row = conn.execute("SELECT id FROM principals LIMIT 1").fetchone()
    if row is not None:
        raise ValueError("bootstrap key does not match any issued key")
    principal_id = create_principal(
        conn,
        tenant_id="*",
        kind="human",
        name="bootstrap-admin",
        roles=["sys_admin", "kb_admin"],
    )
    conn.execute(
        "INSERT INTO api_keys(principal_id, key_hash, key_prefix, scopes, created_by)"
        " VALUES(?,?,?,?,?)",
        (
            principal_id,
            hash_key(raw_key),
            raw_key[:12],
            json.dumps(["read"], ensure_ascii=False),
            principal_id,
        ),
    )
    conn.commit()
    found = lookup_principal(conn, raw_key)
    assert found is not None
    return found


import contextvars as _contextvars

current_principal: _contextvars.ContextVar["Principal | None"] = _contextvars.ContextVar(
    "libref_current_principal", default=None
)
