"""Role separation at the CLI edge: split advice, packing, and explicit repair."""

from __future__ import annotations

import json
import sqlite3

import pytest

from libref.cli.ops_commands import _warn_role_conflicts
from libref.services.infra.auth import (
    ROLE_SCOPES,
    create_principal,
    dev_principal,
    ensure_auth_tables,
    list_principals,
    lookup_principal,
    pack_roles,
    scopes_for_roles,
    separation_advice,
    set_principal_roles,
    split_role_conflict,
)
from tests.conftest import make_settings


def _insert_conflict(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO principals(tenant_id, kind, name, roles) VALUES(?,?,?,?)",
        ("default", "human", "legacy", json.dumps(["developer", "kb_admin"])),
    )
    conn.commit()


def _insert(
    conn: sqlite3.Connection, name: str, roles: list[str], tenant="default"
) -> int:
    cur = conn.execute(
        "INSERT INTO principals(tenant_id, kind, name, roles) VALUES(?,?,?,?)",
        (tenant, "human", name, json.dumps(roles)),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def _roles(conn: sqlite3.Connection, principal_id: int) -> list[str]:
    row = conn.execute(
        "SELECT roles FROM principals WHERE id=?", (principal_id,)
    ).fetchone()
    return json.loads(row[0])


def test_startup_stays_silent_without_conflicts(capsys) -> None:
    conn = sqlite3.connect(":memory:")
    try:
        ensure_auth_tables(conn)
        _warn_role_conflicts(conn)
        assert capsys.readouterr().err == ""
    finally:
        conn.close()


def test_startup_warns_but_does_not_raise(capsys) -> None:
    conn = sqlite3.connect(":memory:")
    try:
        ensure_auth_tables(conn)
        _insert_conflict(conn)
        _warn_role_conflicts(conn)
        stderr = capsys.readouterr().err
        assert "separation of duties" in stderr
        assert "role-audit" in stderr
    finally:
        conn.close()


def test_startup_tolerates_missing_identity_tables(capsys) -> None:
    """An unmigrated database must not turn the advisory into a crash."""
    conn = sqlite3.connect(":memory:")
    try:
        _warn_role_conflicts(conn)
        assert capsys.readouterr().err == ""
    finally:
        conn.close()


def test_advice_offers_one_plan_per_kept_role_and_loses_no_scope() -> None:
    roles = ["developer", "kb_admin"]
    plans = separation_advice(roles)
    assert [plan["keep"] for plan in plans] == ["developer", "kb_admin"]
    expected = set().union(*[ROLE_SCOPES[role] for role in roles])
    for plan in plans:
        assert plan["move"] == [r for r in roles if r != plan["keep"]]
        covered = set(plan["kept_scopes"]) | set(plan["move_scopes"])
        assert covered == expected


def test_advice_is_empty_without_a_conflict() -> None:
    assert separation_advice(["developer", "external"]) == []
    assert separation_advice([]) == []


def test_pack_roles_keeps_the_principal_count_minimal() -> None:
    """A read-only bystander shares a principal instead of getting its own."""
    assert pack_roles(["external", "kb_admin", "sys_admin"]) == [
        ["external", "kb_admin"],
        ["sys_admin"],
    ]
    assert pack_roles(["kb_admin", "sys_admin"]) == [["kb_admin"], ["sys_admin"]]
    assert pack_roles([]) == []


def test_dry_run_reports_the_plan_without_writing(monkeypatch, tmp_path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    conn = sqlite3.connect(":memory:")
    try:
        ensure_auth_tables(conn)
        pid = _insert(conn, "legacy", ["external", "developer", "sys_admin"])
        result = split_role_conflict(
            conn,
            caller=dev_principal(settings),
            settings=settings,
            principal_id=pid,
            keep_role="developer",
            dry_run=True,
        )
        # ``external`` conflicts with nobody, so it rides along with sys_admin.
        assert result["planned"] == [["external", "sys_admin"]]
        assert result["new_principals"] == []
        assert _roles(conn, pid) == ["external", "developer", "sys_admin"]
        assert conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM principals").fetchone()[0] == 1
    finally:
        conn.close()


def test_split_moves_extra_roles_and_issues_working_keys(monkeypatch, tmp_path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    conn = sqlite3.connect(":memory:")
    try:
        ensure_auth_tables(conn)
        pid = _insert(conn, "legacy", ["kb_admin", "sys_admin"])
        result = split_role_conflict(
            conn,
            caller=dev_principal(settings),
            settings=settings,
            principal_id=pid,
            keep_role="kb_admin",
        )
        assert _roles(conn, pid) == ["kb_admin"]
        parts = result["new_principals"]
        assert len(parts) == 1
        assert parts[0]["roles"] == ["sys_admin"]
        assert _roles(conn, parts[0]["principal_id"]) == ["sys_admin"]
        restored = lookup_principal(conn, parts[0]["key"])
        assert restored is not None
        assert restored.has("admin:keys")
        assert not restored.has("review:write")
    finally:
        conn.close()


def test_split_spreads_three_conflicting_roles_over_three_principals(
    monkeypatch, tmp_path
) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    conn = sqlite3.connect(":memory:")
    try:
        ensure_auth_tables(conn)
        pid = _insert(conn, "triple", ["developer", "kb_admin", "sys_admin"])
        result = split_role_conflict(
            conn,
            caller=dev_principal(settings),
            settings=settings,
            principal_id=pid,
            keep_role="developer",
        )
        assert sorted(result["moved_roles"]) == ["kb_admin", "sys_admin"]
        assert len(result["new_principals"]) == 2
        every = [*_roles(conn, pid)] + [
            r for p in result["new_principals"] for r in p["roles"]
        ]
        assert sorted(every) == ["developer", "kb_admin", "sys_admin"]
    finally:
        conn.close()


def test_split_rejects_unknown_keep_role(monkeypatch, tmp_path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    conn = sqlite3.connect(":memory:")
    try:
        ensure_auth_tables(conn)
        pid = _insert(conn, "legacy", ["kb_admin", "sys_admin"])
        with pytest.raises(ValueError, match="is not held"):
            split_role_conflict(
                conn,
                caller=dev_principal(settings),
                settings=settings,
                principal_id=pid,
                keep_role="developer",
            )
    finally:
        conn.close()


def test_split_rejects_principal_without_conflict(monkeypatch, tmp_path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    conn = sqlite3.connect(":memory:")
    try:
        ensure_auth_tables(conn)
        pid = _insert(conn, "fine", ["kb_admin"])
        with pytest.raises(ValueError, match="no conflicting roles"):
            split_role_conflict(
                conn,
                caller=dev_principal(settings),
                settings=settings,
                principal_id=pid,
                keep_role="kb_admin",
            )
    finally:
        conn.close()


def test_split_rejects_roles_the_caller_cannot_grant(monkeypatch, tmp_path) -> None:
    """A tenant admin cannot create a principal holding a role it never had."""
    settings = make_settings(monkeypatch, tmp_path)
    conn = sqlite3.connect(":memory:")
    try:
        ensure_auth_tables(conn)
        caller = dev_principal(settings)
        caller.roles = ["sys_admin"]
        caller.scopes = scopes_for_roles(caller.roles)
        pid = _insert(conn, "mixed", ["kb_admin", "sys_admin"])
        with pytest.raises(ValueError, match="cannot grant roles"):
            split_role_conflict(
                conn,
                caller=caller,
                settings=settings,
                principal_id=pid,
                keep_role="sys_admin",
            )
        # Nothing was written before the rejection.
        assert conn.execute("SELECT COUNT(*) FROM principals").fetchone()[0] == 1
    finally:
        conn.close()


def test_set_principal_roles_applies_the_creation_rules(monkeypatch, tmp_path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    conn = sqlite3.connect(":memory:")
    try:
        ensure_auth_tables(conn)
        caller = dev_principal(settings)
        pid = create_principal(
            conn,
            caller=caller,
            tenant_id="default",
            kind="human",
            name="ops",
            roles=["sys_admin"],
        )
        with pytest.raises(ValueError, match="separation of duties"):
            set_principal_roles(
                conn, caller=caller, principal_id=pid, roles=["sys_admin", "kb_admin"]
            )
        assert set_principal_roles(
            conn, caller=caller, principal_id=pid, roles=["kb_admin"]
        ) == ["kb_admin"]
        assert _roles(conn, pid) == ["kb_admin"]
    finally:
        conn.close()


def test_list_principals_flags_conflicts_and_respects_tenant(
    monkeypatch, tmp_path
) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    conn = sqlite3.connect(":memory:")
    try:
        ensure_auth_tables(conn)
        _insert(conn, "legacy", ["kb_admin", "sys_admin"], tenant="t1")
        _insert(conn, "other", ["developer"], tenant="t2")
        platform = list_principals(conn, viewer=dev_principal(settings))
        assert [row["name"] for row in platform] == ["legacy", "other"]
        flagged = [row for row in platform if row["conflicting"]]
        assert [row["name"] for row in flagged] == ["legacy"]
        assert platform[0]["scopes"] == sorted(
            scopes_for_roles(["kb_admin", "sys_admin"])
        )

        tenant_view = dev_principal(settings)
        tenant_view.tenant_id = "t1"
        tenant_view.roles = ["sys_admin"]
        tenant_view.scopes = scopes_for_roles(tenant_view.roles)
        scoped = list_principals(conn, viewer=tenant_view)
        assert [row["name"] for row in scoped] == ["legacy"]
    finally:
        conn.close()
