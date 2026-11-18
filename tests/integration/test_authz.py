"""Authorization integration tests: scopes, tenant bounds, origins, audit trail."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from libref.db import apply_migrations, open_db
from libref.services.infra.auth import (
    PLATFORM_ROLE,
    create_principal,
    dev_principal,
    issue_api_key,
    revoke_principal_keys,
)
from libref.web import build_web_app
from tests.conftest import make_settings

pytestmark = pytest.mark.integration


def _setup(tmp_path, monkeypatch, **overrides):
    db_path = tmp_path / "authz.db"
    conn = open_db(db_path, check_same_thread=False)
    apply_migrations(conn)
    defaults = {
        "auth_mode": "enforced",
        "allowed_origins": ["https://app.internal"],
    }
    defaults.update(overrides)
    settings = make_settings(monkeypatch, tmp_path, db_path=str(db_path), **defaults)
    client = TestClient(build_web_app(conn, settings, context=None))
    return conn, settings, client


def _key(conn, settings, *, name, roles, tenant_id="default", scopes=None):
    caller = dev_principal(settings)
    pid = create_principal(
        conn,
        caller=caller,
        tenant_id=tenant_id,
        kind="human",
        name=name,
        roles=roles,
    )
    _, raw = issue_api_key(
        conn,
        caller=caller,
        settings=settings,
        principal_id=pid,
        scopes=scopes,
    )
    return pid, raw


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def test_missing_key_and_bad_origin_are_audited(tmp_path, monkeypatch) -> None:
    conn, _, client = _setup(tmp_path, monkeypatch)
    try:
        missing = client.get("/catalog")
        assert missing.status_code == 401
        assert missing.json()["error"] == "unauthorized"

        wrong_origin = client.get(
            "/catalog",
            headers={**_auth("libref_bad"), "Origin": "https://evil.example"},
        )
        assert wrong_origin.status_code == 403
        assert wrong_origin.json()["error"] == "origin_forbidden"

        rows = conn.execute(
            "SELECT action, result FROM audit_log ORDER BY id"
        ).fetchall()
        assert ("auth.denied", "unauthorized") in rows
        assert ("auth.denied", "origin_forbidden") in rows
    finally:
        conn.close()


def test_developer_scope_denial_is_audited(tmp_path, monkeypatch) -> None:
    conn, settings, client = _setup(tmp_path, monkeypatch)
    try:
        _, raw = _key(conn, settings, name="dev", roles=["developer"])
        assert client.get("/catalog", headers=_auth(raw)).status_code == 200
        denied = client.get("/review", params={"action": "list"}, headers=_auth(raw))
        assert denied.status_code == 403
        assert denied.json()["error"] == "missing_scope"

        rows = conn.execute(
            "SELECT action, result FROM audit_log ORDER BY id"
        ).fetchall()
        assert ("auth.forbidden", "denied") in rows
    finally:
        conn.close()


def test_admin_cannot_grant_beyond_own_tenant_or_scopes(tmp_path, monkeypatch) -> None:
    conn, settings, client = _setup(tmp_path, monkeypatch)
    try:
        _, admin_key = _key(conn, settings, name="sys", roles=["sys_admin"])
        global_body = {
            "name": "global",
            "tenant_id": "*",
            "roles": [PLATFORM_ROLE],
        }
        denied = client.post("/admin/keys", json=global_body, headers=_auth(admin_key))
        assert denied.status_code == 400

        role_body = {
            "name": "hidden-root",
            "tenant_id": "default",
            "roles": [PLATFORM_ROLE],
        }
        assert (
            client.post(
                "/admin/keys", json=role_body, headers=_auth(admin_key)
            ).status_code
            == 400
        )

        scope_body = {
            "name": "reviewer",
            "tenant_id": "default",
            "roles": [],
            "scopes": ["review:write"],
        }
        assert (
            client.post(
                "/admin/keys", json=scope_body, headers=_auth(admin_key)
            ).status_code
            == 400
        )
        _, platform_key = _key(conn, settings, name="root", roles=[PLATFORM_ROLE])
        allowed = client.post(
            "/admin/keys",
            json={**global_body, "name": "global-ok"},
            headers=_auth(platform_key),
        )
        assert allowed.status_code == 200
    finally:
        conn.close()


def test_key_listing_is_tenant_scoped_and_revoke_all_works(
    tmp_path, monkeypatch
) -> None:
    conn, settings, client = _setup(tmp_path, monkeypatch)
    try:
        tenant_pid, tenant_key = _key(
            conn, settings, name="tenant-dev", roles=["developer"], tenant_id="t1"
        )
        _, platform_key = _key(conn, settings, name="root", roles=[PLATFORM_ROLE])

        _, root_key = _key(conn, settings, name="root-2", roles=[PLATFORM_ROLE])
        global_rows = client.get("/admin/keys", headers=_auth(root_key)).json()
        assert global_rows["count"] >= 3

        assert (
            client.delete(
                f"/admin/principals/{tenant_pid}/keys", headers=_auth(platform_key)
            ).json()["revoked"]
            == 1
        )
        assert client.get("/catalog", headers=_auth(tenant_key)).status_code == 401
    finally:
        conn.close()


def test_config_refresh_failure_is_visible_and_audited(tmp_path, monkeypatch) -> None:
    conn, settings, client = _setup(tmp_path, monkeypatch)
    try:
        _, raw = _key(conn, settings, name="root", roles=[PLATFORM_ROLE])

        def _boom():
            raise ValueError("bad config")

        import libref.config as config_module

        monkeypatch.setattr(config_module, "load_settings", _boom)
        res = client.post("/admin/config/refresh", headers=_auth(raw))
        assert res.status_code == 400
        assert "config refresh failed" in res.json()["detail"]
        rows = conn.execute(
            "SELECT action, result FROM audit_log WHERE action='config.refresh'"
        ).fetchall()
        assert rows == [("config.refresh", "denied")]
    finally:
        conn.close()


def test_revoke_principal_keys_direct_call(tmp_path, monkeypatch) -> None:
    conn, settings, _ = _setup(tmp_path, monkeypatch)
    try:
        pid, raw = _key(conn, settings, name="dev", roles=["developer"])
        assert (
            revoke_principal_keys(
                conn, caller=dev_principal(settings), principal_id=pid
            )
            == 1
        )
        from libref.services.infra.auth import lookup_principal

        assert lookup_principal(conn, raw) is None
    finally:
        conn.close()
