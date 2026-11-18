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
from libref.services.infra.jobs import JobManager
from libref.services.ops.feedback import FeedbackService
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


def test_admin_keys_rejects_conflicting_roles(tmp_path, monkeypatch) -> None:
    """A single key cannot carry roles that break separation of duties."""
    conn, settings, client = _setup(tmp_path, monkeypatch)
    try:
        _, root_key = _key(conn, settings, name="root", roles=[PLATFORM_ROLE])
        body = {
            "name": "combined",
            "tenant_id": "default",
            "roles": ["kb_admin", "sys_admin"],
        }
        denied = client.post("/admin/keys", json=body, headers=_auth(root_key))
        assert denied.status_code == 400
        assert "separation of duties" in denied.json()["detail"]
        created = conn.execute(
            "SELECT COUNT(*) FROM principals WHERE name='combined'"
        ).fetchone()[0]
        assert created == 0
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


def test_feedback_resolve_is_tenant_scoped(tmp_path, monkeypatch) -> None:
    conn, settings, client = _setup(tmp_path, monkeypatch)
    try:
        _, tenant_key = _key(
            conn, settings, name="reviewer", roles=["kb_admin"], tenant_id="t1"
        )
        foreign_pid, _ = _key(
            conn, settings, name="foreign", roles=["developer"], tenant_id="t2"
        )
        feedback_id = FeedbackService(conn).submit(
            tenant_id="t2", principal_id=foreign_pid, body="foreign feedback"
        )
        denied = client.post(
            f"/feedback/{feedback_id}/resolve", headers=_auth(tenant_key)
        )
        assert denied.status_code == 404
        status = conn.execute(
            "SELECT status FROM feedback WHERE id=?", (feedback_id,)
        ).fetchone()[0]
        assert status == "open"
        _, platform_key = _key(conn, settings, name="root", roles=[PLATFORM_ROLE])
        assert (
            client.post(
                f"/feedback/{feedback_id}/resolve", headers=_auth(platform_key)
            ).status_code
            == 200
        )
    finally:
        conn.close()


def test_job_cancel_is_tenant_scoped(tmp_path, monkeypatch) -> None:
    conn, settings, client = _setup(tmp_path, monkeypatch)
    try:
        _, tenant_key = _key(
            conn, settings, name="sys", roles=["sys_admin"], tenant_id="t1"
        )
        job_id = JobManager(conn).submit("probe", {}, tenant_id="t2")
        denied = client.post(f"/jobs/{job_id}/cancel", headers=_auth(tenant_key))
        assert denied.status_code == 404
        flag = conn.execute(
            "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)
        ).fetchone()[0]
        assert flag == 0
        _, platform_key = _key(conn, settings, name="root", roles=[PLATFORM_ROLE])
        assert (
            client.post(
                f"/jobs/{job_id}/cancel", headers=_auth(platform_key)
            ).status_code
            == 200
        )
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


def test_developer_reads_only_own_feedback(tmp_path, monkeypatch) -> None:
    conn, settings, client = _setup(tmp_path, monkeypatch)
    try:
        author_pid, author_key = _key(conn, settings, name="dev-a", roles=["developer"])
        _, other_key = _key(conn, settings, name="dev-b", roles=["developer"])
        _, reviewer_key = _key(conn, settings, name="kb", roles=["kb_admin"])
        _, reader_key = _key(conn, settings, name="bot", roles=["external"])

        for key, body in ((author_key, "from a"), (other_key, "from b")):
            created = client.post("/feedback", json={"body": body}, headers=_auth(key))
            assert created.status_code == 200

        mine = client.get("/feedback", headers=_auth(author_key)).json()
        assert mine["count"] == 1
        assert mine["items"][0]["principal_id"] == author_pid
        assert mine["items"][0]["body"] == "from a"

        everything = client.get("/feedback", headers=_auth(reviewer_key)).json()
        assert everything["count"] == 2

        denied = client.get("/feedback", headers=_auth(reader_key))
        assert denied.status_code == 403
        assert denied.json()["error"] == "missing_scope"
    finally:
        conn.close()


def test_quota_policy_routes_respect_scope_and_tenant(tmp_path, monkeypatch) -> None:
    conn, settings, client = _setup(tmp_path, monkeypatch)
    try:
        _, ops_key = _key(conn, settings, name="ops", roles=["sys_admin"])
        _, dev_key = _key(conn, settings, name="dev", roles=["developer"])
        _, foreign_key = _key(
            conn, settings, name="ops-t2", roles=["sys_admin"], tenant_id="t2"
        )
        payload = {
            "resource": "http_requests",
            "limit_value": 7,
            "window_type": "hour",
        }

        assert (
            client.put(
                "/admin/quota/policies", json=payload, headers=_auth(dev_key)
            ).status_code
            == 403
        )
        crossed = client.put(
            "/admin/quota/policies",
            json={**payload, "tenant_id": "default"},
            headers=_auth(foreign_key),
        )
        assert crossed.status_code == 403

        assert (
            client.put(
                "/admin/quota/policies", json=payload, headers=_auth(ops_key)
            ).status_code
            == 200
        )
        listed = client.get("/admin/quota/policies", headers=_auth(ops_key)).json()
        assert listed["count"] == 1
        row = listed["items"][0]
        assert row["tenant_id"] == "default"
        assert row["principal_id"] is None
        assert (row["limit_value"], row["window_type"]) == (7, "hour")

        from libref.services.infra import quota

        assert quota.effective_policy(
            conn,
            tenant_id="default",
            principal_id=None,
            resource="http_requests",
            settings=settings,
        ) == (7, "hour")

        unknown = client.put(
            "/admin/quota/policies",
            json={"resource": "nope", "limit_value": 1},
            headers=_auth(ops_key),
        )
        assert unknown.status_code == 400

        rows = conn.execute(
            "SELECT target FROM audit_log WHERE action='quota.policy'"
        ).fetchall()
        assert rows == [("default:*:http_requests",)]
    finally:
        conn.close()


def test_principals_view_flags_conflicts_and_needs_admin_scope(
    tmp_path, monkeypatch
) -> None:
    """The read-only principal view is the browser counterpart of role-audit."""
    conn, settings, client = _setup(tmp_path, monkeypatch)
    try:
        _, root_key = _key(conn, settings, name="root", roles=[PLATFORM_ROLE])
        _, dev_key = _key(conn, settings, name="dev", roles=["developer"])
        # Insert directly: this models a row written before the rule existed,
        # which the service layer would now refuse to create.
        conn.execute(
            "INSERT INTO principals(tenant_id, kind, name, roles) VALUES(?,?,?,?)",
            ("default", "human", "legacy", '["kb_admin", "sys_admin"]'),
        )
        conn.commit()

        denied = client.get("/admin/principals", headers=_auth(dev_key))
        assert denied.status_code == 403
        assert denied.json()["error"] == "missing_scope"

        ok = client.get("/admin/principals", headers=_auth(root_key))
        assert ok.status_code == 200
        rows = {row["name"]: row for row in ok.json()["items"]}
        assert rows["legacy"]["conflicting"] == [["kb_admin", "sys_admin"]]
        assert rows["dev"]["conflicting"] == []
        assert "review:read" not in rows["dev"]["scopes"]

        actions = [
            row[0] for row in conn.execute("SELECT action FROM audit_log ORDER BY id")
        ]
        assert "principals.list" in actions
    finally:
        conn.close()


def test_platform_role_issue_gets_its_own_audit_action(tmp_path, monkeypatch) -> None:
    """Granting the separated-out platform role must stand out in the trail."""
    conn, settings, client = _setup(tmp_path, monkeypatch)
    try:
        _, root_key = _key(conn, settings, name="root", roles=[PLATFORM_ROLE])
        created = client.post(
            "/admin/keys",
            json={
                "name": "second-root",
                "tenant_id": "default",
                "roles": [PLATFORM_ROLE],
            },
            headers=_auth(root_key),
        )
        assert created.status_code == 200
        tenant_key = client.post(
            "/admin/keys",
            json={"name": "ops", "tenant_id": "default", "roles": ["sys_admin"]},
            headers=_auth(root_key),
        )
        assert tenant_key.status_code == 200
        actions = [
            row[0] for row in conn.execute("SELECT action FROM audit_log ORDER BY id")
        ]
        assert "keys.issue.platform" in actions
        assert "keys.issue" in actions
    finally:
        conn.close()
