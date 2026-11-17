"""Principal auth unit tests: bootstrap roles, key expiry, role validation, status gate."""

from __future__ import annotations

import sqlite3
import time

import pytest

from libref.services.infra.auth import (
    bootstrap_admin,
    create_principal,
    issue_api_key,
    lookup_principal,
)


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


def test_bootstrap_admin_covers_all_frontend_roles() -> None:
    conn = _conn()
    try:
        key = "libref_" + "b" * 48
        admin = bootstrap_admin(conn, key)
        assert {"sys_admin", "kb_admin", "developer"} <= set(admin.roles)
        assert {"read", "review:write", "feedback:write", "admin:keys"} <= admin.scopes
    finally:
        conn.close()


def test_expired_key_misses() -> None:
    conn = _conn()
    try:
        pid = create_principal(
            conn, tenant_id="default", kind="human", name="dev", roles=["developer"]
        )
        _, raw = issue_api_key(conn, principal_id=pid, expires_at=time.time() - 1.0)
        assert lookup_principal(conn, raw) is None
        _, fresh = issue_api_key(
            conn, principal_id=pid, expires_at=time.time() + 3600.0
        )
        assert lookup_principal(conn, fresh) is not None
    finally:
        conn.close()


def test_unknown_role_rejected() -> None:
    conn = _conn()
    try:
        with pytest.raises(ValueError, match="unknown roles"):
            create_principal(
                conn,
                tenant_id="default",
                kind="human",
                name="oops",
                roles=["develpoer"],
            )
    finally:
        conn.close()


def test_inactive_principal_misses() -> None:
    conn = _conn()
    try:
        pid = create_principal(
            conn, tenant_id="default", kind="human", name="dev", roles=["developer"]
        )
        _, raw = issue_api_key(conn, principal_id=pid)
        assert lookup_principal(conn, raw) is not None
        conn.execute("UPDATE principals SET status='disabled' WHERE id=?", (pid,))
        conn.commit()
        assert lookup_principal(conn, raw) is None
    finally:
        conn.close()


def test_lookup_records_last_used() -> None:
    conn = _conn()
    try:
        pid = create_principal(
            conn, tenant_id="default", kind="human", name="dev", roles=["developer"]
        )
        key_id, raw = issue_api_key(conn, principal_id=pid)
        before = conn.execute(
            "SELECT last_used_at FROM api_keys WHERE id=?", (key_id,)
        ).fetchone()[0]
        assert before is None
        assert lookup_principal(conn, raw) is not None
        after = conn.execute(
            "SELECT last_used_at FROM api_keys WHERE id=?", (key_id,)
        ).fetchone()[0]
        assert after is not None
    finally:
        conn.close()
