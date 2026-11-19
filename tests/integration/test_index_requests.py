"""Index-request closed loop over HTTP: approval gating, job writeback, and bypass denial."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from libref.db import apply_migrations, open_db
from libref.services.infra import quota
from libref.services.infra.auth import (
    create_principal,
    dev_principal,
    issue_api_key,
)
from libref.services.infra.jobs import JobManager
from libref.services.ops.index_requests import (
    IndexRequestError,
    IndexRequestService,
    build_index_request_runner,
)
from libref.web import build_web_app
from tests.conftest import make_settings

pytestmark = pytest.mark.integration

_DOCS = {
    "kind": "docs",
    "library": "demo",
    "version": "1.0",
    "ecosystem": "custom",
    "start_url": "https://docs.example.com/guide",
}


def _setup(tmp_path, monkeypatch, **overrides):
    db_path = tmp_path / "requests.db"
    conn = open_db(db_path, check_same_thread=False)
    apply_migrations(conn)
    settings = make_settings(
        monkeypatch,
        tmp_path,
        db_path=str(db_path),
        auth_mode="enforced",
        **overrides,
    )
    client = TestClient(build_web_app(conn, settings, context=None))
    return conn, settings, client


def _key(conn, settings, name, roles):
    caller = dev_principal(settings)
    pid = create_principal(
        conn,
        caller=caller,
        tenant_id="default",
        kind="human",
        name=name,
        roles=roles,
    )
    _, raw = issue_api_key(conn, caller=caller, settings=settings, principal_id=pid)
    return pid, {"Authorization": f"Bearer {raw}"}


def _complete_one_job(conn, settings) -> None:
    jobs = JobManager(conn)
    job = jobs.claim_next()
    assert job is not None
    runner = build_index_request_runner(
        settings, conn, lambda _job: {"pages": 1}, jobs=jobs
    )
    asyncio.run(runner(job))
    jobs._update(job.id, status="done", result=json.dumps({"pages": 1}))


def test_low_privilege_request_waits_for_approval_and_writeback(
    tmp_path, monkeypatch
) -> None:
    conn, settings, client = _setup(tmp_path, monkeypatch)
    try:
        _, requester = _key(conn, settings, "requester", ["external"])
        _, approver = _key(conn, settings, "approver", ["kb_admin"])
        created = client.post("/index-requests", json=_DOCS, headers=requester)
        assert created.status_code == 200, created.text
        item = created.json()
        assert item["status"] == "pending"
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0

        queued = client.post(f"/index-requests/{item['id']}/approve", headers=approver)
        assert queued.status_code == 200, queued.text
        assert queued.json()["status"] == "queued"
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1

        _complete_one_job(conn, settings)
        detail = client.get(f"/index-requests/{item['id']}", headers=requester)
        assert detail.status_code == 200
        assert detail.json()["status"] == "done"

        queue = client.get("/index-requests/pending", headers=approver).json()
        assert queue["items"] == []
    finally:
        conn.close()


def test_reject_and_cancel_create_no_jobs(tmp_path, monkeypatch) -> None:
    conn, settings, client = _setup(tmp_path, monkeypatch)
    try:
        _, requester = _key(conn, settings, "requester", ["developer"])
        _, approver = _key(conn, settings, "approver", ["kb_admin"])
        first = client.post("/index-requests", json=_DOCS, headers=requester).json()
        second_url = f"{_DOCS['start_url']}/other"
        second = client.post(
            "/index-requests",
            json={**_DOCS, "start_url": second_url},
            headers=requester,
        ).json()
        rejected = client.post(
            f"/index-requests/{first['id']}/reject",
            json={"reason": "out of scope"},
            headers=approver,
        )
        assert rejected.json()["status"] == "rejected"
        assert (
            client.post(
                f"/index-requests/{second['id']}/cancel", headers=requester
            ).json()["status"]
            == "cancelled"
        )
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    finally:
        conn.close()


def test_generic_jobs_endpoint_cannot_bypass_approval(tmp_path, monkeypatch) -> None:
    conn, settings, client = _setup(tmp_path, monkeypatch)
    try:
        _, admin = _key(conn, settings, "sys", ["sys_admin"])
        for kind in ("scrape_docs", "index_repo"):
            response = client.post(
                "/jobs", json={"kind": kind, "params": {}}, headers=admin
            )
            assert response.status_code == 400
            assert response.json()["error"] == "invalid_argument"
        assert (
            client.post(
                "/jobs", json={"kind": "mine_patterns", "params": {}}, headers=admin
            ).status_code
            == 200
        )
    finally:
        conn.close()


def test_quota_rejection_carries_resource_and_reset(tmp_path, monkeypatch) -> None:
    quota.invalidate_policy_cache()
    conn, settings, client = _setup(
        tmp_path,
        monkeypatch,
        quota_enabled=True,
        quota_index_requests_per_hour=1,
    )
    try:
        _, requester = _key(conn, settings, "requester", ["external"])
        assert (
            client.post("/index-requests", json=_DOCS, headers=requester).status_code
            == 200
        )
        denied = client.post(
            "/index-requests",
            json={**_DOCS, "start_url": f"{_DOCS['start_url']}/other"},
            headers=requester,
        )
        assert denied.status_code == 429, denied.text
        body = denied.json()
        assert body["error"] == "quota_exceeded"
        assert body["resource"] == "index_requests"
        assert body["reset_at"]
    finally:
        quota.invalidate_policy_cache()
        conn.close()


def test_service_listing_uses_tenant_and_owner_rules(tmp_path, monkeypatch) -> None:
    conn, settings, _ = _setup(tmp_path, monkeypatch)
    try:
        svc = IndexRequestService(conn, settings, jobs=JobManager(conn))
        actor = dev_principal(settings)
        item = asyncio.run(svc.submit(_DOCS, actor))
        assert [row["id"] for row in svc.list(actor)] == [item["id"]]
        from libref.services.infra.auth import Principal

        other = Principal(
            id=999, tenant_id="default", name="other", scopes={"read", "index:request"}
        )
        assert svc.list(other) == []
        with pytest.raises(IndexRequestError, match="index:approve"):
            svc.list(other, pending=True)
    finally:
        conn.close()
