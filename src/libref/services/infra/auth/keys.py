"""API key hashing, issuance, lookup and revocation."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import TypedDict

from libref.db.contract import MetadataSession
from libref.services.infra.auth.identity import (
    KNOWN_SCOPES,
    PLATFORM_ROLE,
    PLATFORM_SCOPE,
    ROLE_SCOPES,
    Principal,
    _parse_json_list,
    scopes_for_roles,
)
from libref.services.infra.auth.storage import (
    _principal_from_row,
    _principal_row,
    ensure_auth_tables,
)
from libref.services.infra.timestamps import iso_utc


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


class ApiKeySummary(TypedDict):
    """Read-only key row for the admin view; never carries key material."""

    id: int
    principal_id: int
    key_prefix: str
    scopes: list[str]
    expires_at: str | None
    revoked_at: str | None
    last_used_at: str | None
    created_at: str | None


def list_api_keys(
    conn: MetadataSession, *, viewer: Principal, tenant_id: str | None = None
) -> list[ApiKeySummary]:
    """List keys visible to a viewer, optionally narrowed to one tenant."""
    wanted = (tenant_id or "").strip() or None
    if PLATFORM_SCOPE in viewer.scopes:
        filter_tenant = wanted
    else:
        if wanted and wanted != viewer.tenant_id:
            raise ValueError("caller cannot administer target tenant")
        filter_tenant = viewer.tenant_id
    sql = (
        "SELECT k.id, k.principal_id, k.key_prefix, k.scopes, k.expires_at,"
        " k.revoked_at, k.last_used_at, k.created_at FROM api_keys k"
        " JOIN principals p ON p.id=k.principal_id"
    )
    params: tuple = ()
    if filter_tenant:
        sql += " WHERE p.tenant_id=?"
        params = (filter_tenant,)
    sql += " ORDER BY k.id"
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": int(r[0]),
            "principal_id": int(r[1]),
            "key_prefix": str(r[2]),
            "scopes": _parse_json_list(str(r[3])),
            "expires_at": iso_utc(r[4]),
            "revoked_at": iso_utc(r[5]),
            "last_used_at": iso_utc(r[6]),
            "created_at": iso_utc(r[7]),
        }
        for r in rows
    ]


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
