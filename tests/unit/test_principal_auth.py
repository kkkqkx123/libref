"""Principal auth unit tests: bootstrap, scope bounds, tenant bounds, key lifetime."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

import pytest

from libref.services.infra.auth import (
    PLATFORM_ROLE,
    PLATFORM_SCOPE,
    bootstrap_admin,
    create_principal,
    dev_principal,
    ensure_auth_tables,
    issue_api_key,
    lookup_principal,
    revoke_principal_keys,
    scan_role_conflicts,
    scopes_for_roles,
)
from tests.conftest import make_settings


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


def _settings(monkeypatch, tmp_path, **overrides):
    return make_settings(monkeypatch, tmp_path, **overrides)


def _caller(settings, *, roles=None, tenant_id="default"):
    principal = dev_principal(settings)
    principal.tenant_id = tenant_id
    principal.roles = list(roles or [PLATFORM_ROLE])
    principal.scopes = scopes_for_roles(principal.roles)
    return principal


def test_bootstrap_admin_has_platform_scope(monkeypatch, tmp_path) -> None:
    conn = _conn()
    settings = _settings(monkeypatch, tmp_path)
    try:
        key = "libref_" + "b" * 48
        admin = bootstrap_admin(conn, key, settings)
        assert PLATFORM_ROLE in admin.roles
        assert {
            "read",
            "review:write",
            "feedback:write",
            "admin:keys",
            "platform:admin",
        } <= admin.scopes
    finally:
        conn.close()


def test_expired_key_misses(monkeypatch, tmp_path) -> None:
    conn = _conn()
    settings = _settings(monkeypatch, tmp_path)
    try:
        caller = _caller(settings)
        pid = create_principal(
            conn,
            caller=caller,
            tenant_id="default",
            kind="human",
            name="dev",
            roles=["developer"],
        )
        _, raw = issue_api_key(
            conn,
            caller=caller,
            settings=settings,
            principal_id=pid,
            expires_at=time.time() - 1.0,
        )
        assert lookup_principal(conn, raw) is None
        _, fresh = issue_api_key(
            conn,
            caller=caller,
            settings=settings,
            principal_id=pid,
            expires_at=time.time() + 3600.0,
        )
        assert lookup_principal(conn, fresh) is not None
    finally:
        conn.close()


def test_unknown_role_and_scope_rejected(monkeypatch, tmp_path) -> None:
    conn = _conn()
    settings = _settings(monkeypatch, tmp_path)
    try:
        caller = _caller(settings)
        with pytest.raises(ValueError, match="unknown roles"):
            create_principal(
                conn,
                caller=caller,
                tenant_id="default",
                kind="human",
                name="oops",
                roles=["develpoer"],
            )
        pid = create_principal(
            conn,
            caller=caller,
            tenant_id="default",
            kind="human",
            name="dev",
            roles=["developer"],
        )
        with pytest.raises(ValueError, match="unknown scopes"):
            issue_api_key(
                conn,
                caller=caller,
                settings=settings,
                principal_id=pid,
                scopes=["review:wrote"],
            )
        with pytest.raises(ValueError, match="cannot grant scopes"):
            issue_api_key(
                conn,
                caller=_caller(settings, roles=["sys_admin"]),
                settings=settings,
                principal_id=pid,
                scopes=["review:write"],
            )
    finally:
        conn.close()


def test_tenant_escalation_rejected(monkeypatch, tmp_path) -> None:
    conn = _conn()
    settings = _settings(monkeypatch, tmp_path)
    try:
        sys_admin = _caller(settings, roles=["sys_admin"])
        with pytest.raises(ValueError, match="target tenant"):
            create_principal(
                conn,
                caller=sys_admin,
                tenant_id="*",
                kind="human",
                name="global",
                roles=["sys_admin"],
            )
        with pytest.raises(ValueError, match="target tenant"):
            create_principal(
                conn,
                caller=sys_admin,
                tenant_id="other",
                kind="human",
                name="other",
                roles=["developer"],
            )
    finally:
        conn.close()


def test_inactive_principal_misses(monkeypatch, tmp_path) -> None:
    conn = _conn()
    settings = _settings(monkeypatch, tmp_path)
    try:
        caller = _caller(settings)
        pid = create_principal(
            conn,
            caller=caller,
            tenant_id="default",
            kind="human",
            name="dev",
            roles=["developer"],
        )
        _, raw = issue_api_key(conn, caller=caller, settings=settings, principal_id=pid)
        assert lookup_principal(conn, raw) is not None
        conn.execute("UPDATE principals SET status='disabled' WHERE id=?", (pid,))
        conn.commit()
        assert lookup_principal(conn, raw) is None
    finally:
        conn.close()


def test_lookup_records_last_used(monkeypatch, tmp_path) -> None:
    conn = _conn()
    settings = _settings(monkeypatch, tmp_path)
    try:
        caller = _caller(settings)
        pid = create_principal(
            conn,
            caller=caller,
            tenant_id="default",
            kind="human",
            name="dev",
            roles=["developer"],
        )
        key_id, raw = issue_api_key(
            conn, caller=caller, settings=settings, principal_id=pid
        )
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


def test_key_lifetime_bounds_and_revoke_all(monkeypatch, tmp_path) -> None:
    conn = _conn()
    settings = _settings(
        monkeypatch,
        tmp_path,
        api_key_default_ttl_hours=1.0,
        api_key_max_ttl_hours=2.0,
    )
    try:
        caller = _caller(settings)
        pid = create_principal(
            conn,
            caller=caller,
            tenant_id="default",
            kind="human",
            name="dev",
            roles=["developer"],
        )
        _, raw = issue_api_key(conn, caller=caller, settings=settings, principal_id=pid)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        expires = conn.execute(
            "SELECT expires_at FROM api_keys WHERE key_hash=?", (digest,)
        ).fetchone()[0]
        assert expires <= time.time() + 3600.0 + 1.0
        with pytest.raises(ValueError, match="maximum key lifetime"):
            issue_api_key(
                conn,
                caller=caller,
                settings=settings,
                principal_id=pid,
                expires_at=time.time() + 3.0 * 3600.0,
            )
        assert revoke_principal_keys(conn, caller=caller, principal_id=pid) == 1
        assert lookup_principal(conn, raw) is None
    finally:
        conn.close()


def test_key_scopes_narrow_role_scopes(monkeypatch, tmp_path) -> None:
    conn = _conn()
    settings = _settings(monkeypatch, tmp_path)
    try:
        caller = _caller(settings)
        pid = create_principal(
            conn,
            caller=caller,
            tenant_id="default",
            kind="human",
            name="narrow-admin",
            roles=[PLATFORM_ROLE],
        )
        _, raw = issue_api_key(
            conn, caller=caller, settings=settings, principal_id=pid, scopes=["read"]
        )
        found = lookup_principal(conn, raw)
        assert found is not None
        assert found.scopes == {"read"}
        assert PLATFORM_SCOPE not in found.scopes
    finally:
        conn.close()


def test_key_scopes_cannot_exceed_target_roles(monkeypatch, tmp_path) -> None:
    conn = _conn()
    settings = _settings(monkeypatch, tmp_path)
    try:
        caller = _caller(settings)
        pid = create_principal(
            conn,
            caller=caller,
            tenant_id="default",
            kind="human",
            name="plain-dev",
            roles=["developer"],
        )
        with pytest.raises(ValueError, match="cannot grant scopes"):
            issue_api_key(
                conn,
                caller=caller,
                settings=settings,
                principal_id=pid,
                scopes=["review:write"],
            )
    finally:
        conn.close()


def test_conflicting_tenant_roles_are_rejected(monkeypatch, tmp_path) -> None:
    """Submit, review, and operate must not collapse onto one principal."""
    conn = _conn()
    settings = _settings(monkeypatch, tmp_path)
    try:
        caller = _caller(settings)
        for roles in (
            ["developer", "kb_admin"],
            ["developer", "sys_admin"],
            ["kb_admin", "sys_admin"],
            ["developer", "kb_admin", "sys_admin"],
        ):
            with pytest.raises(ValueError, match="separation of duties"):
                create_principal(
                    conn,
                    caller=caller,
                    tenant_id="default",
                    kind="human",
                    name="mixed",
                    roles=roles,
                )
        assert conn.execute("SELECT COUNT(*) FROM principals").fetchone()[0] == 0
    finally:
        conn.close()


def test_read_only_and_platform_roles_pass_separation(monkeypatch, tmp_path) -> None:
    """``external`` is a consumer, and ``platform_admin`` is not a tenant function."""
    conn = _conn()
    settings = _settings(monkeypatch, tmp_path)
    try:
        caller = _caller(settings)
        allowed = (
            ["developer", "external"],
            ["kb_admin", "external"],
            ["sys_admin", "external"],
            [PLATFORM_ROLE],
            [PLATFORM_ROLE, "developer"],
            [PLATFORM_ROLE, "kb_admin", "sys_admin"],
        )
        for index, roles in enumerate(allowed):
            pid = create_principal(
                conn,
                caller=caller,
                tenant_id="default",
                kind="human",
                name=f"ok-{index}",
                roles=roles,
            )
            assert pid > 0
    finally:
        conn.close()


def test_role_conflict_scan_reports_without_rewriting() -> None:
    """Rows written before the rule existed are reported, never silently narrowed."""
    conn = _conn()
    try:
        ensure_auth_tables(conn)
        conn.execute(
            "INSERT INTO principals(tenant_id, kind, name, roles) VALUES(?,?,?,?)",
            ("default", "human", "legacy", json.dumps(["kb_admin", "sys_admin"])),
        )
        conn.commit()
        conflicts = scan_role_conflicts(conn)
        assert [item["name"] for item in conflicts] == ["legacy"]
        assert conflicts[0]["groups"] == [["kb_admin", "sys_admin"]]
        stored = conn.execute(
            "SELECT roles FROM principals WHERE name='legacy'"
        ).fetchone()[0]
        assert json.loads(stored) == ["kb_admin", "sys_admin"]
    finally:
        conn.close()
