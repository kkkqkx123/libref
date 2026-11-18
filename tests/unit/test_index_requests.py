"""Unit coverage for index-request normalization, dedupe, and state transitions."""

from __future__ import annotations

import asyncio

import pytest

from libref.services.infra.auth import ROLE_SCOPES, Principal
from libref.services.infra.jobs import JobManager
from libref.services.ops.index_requests import (
    IndexRequestError,
    IndexRequestService,
    build_index_request_runner,
)
from tests.conftest import make_settings

pytestmark = pytest.mark.unit


def _principal(pid: int, name: str, scopes: set[str]) -> Principal:
    return Principal(id=pid, name=name, tenant_id="default", scopes=set(scopes))


def _svc(conn, settings, jobs=None):
    return IndexRequestService(conn, settings, jobs=jobs or JobManager(conn))


def _repo_payload(source="https://github.com/org/repo.git"):
    return {
        "kind": "repo",
        "library": "demo",
        "version": "1.0",
        "ecosystem": "custom",
        "source": source,
    }


def _docs_payload(url="https://docs.example.com/guide"):
    return {
        "kind": "docs",
        "library": "demo",
        "version": "1.0",
        "ecosystem": "custom",
        "start_url": url,
    }


def test_role_scope_expansion_includes_request_flow() -> None:
    assert {"index:request"} <= ROLE_SCOPES["external"]
    assert {"index:request"} <= ROLE_SCOPES["developer"]
    assert {"index:request", "index:approve"} <= ROLE_SCOPES["kb_admin"]
    assert {"index:request", "index:approve", "ingest:write"} <= ROLE_SCOPES[
        "sys_admin"
    ]


def test_low_privilege_request_is_pending_and_deduped(
    conn, monkeypatch, tmp_path
) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    svc = _svc(conn, settings)
    actor = _principal(7, "developer", {"read", "index:request"})
    first = asyncio.run(svc.submit(_repo_payload(), actor))
    again = asyncio.run(svc.submit(_repo_payload(), actor))
    assert first["status"] == "pending"
    assert first["job_id"] is None
    assert again["id"] == first["id"]
    assert len(svc.list(actor)) == 1
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_privileged_self_approval_queues_immediately(
    conn, monkeypatch, tmp_path
) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    svc = _svc(conn, settings)
    admin = _principal(
        3, "sys", {"read", "index:request", "index:approve", "ingest:write"}
    )
    item = asyncio.run(svc.submit(_docs_payload(), admin))
    assert item["status"] == "queued"
    assert item["job_id"] is not None
    actions = [row[0] for row in conn.execute("SELECT action FROM audit_log")]
    assert "index_request.auto_approve" in actions

    again = asyncio.run(svc.submit(_docs_payload(), admin))
    assert again["id"] == item["id"]


def test_approver_cannot_approve_own_request(conn, monkeypatch, tmp_path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    svc = _svc(conn, settings)
    admin = _principal(5, "kb", {"read", "index:request", "index:approve"})
    item = asyncio.run(svc.submit(_repo_payload(), admin))
    with pytest.raises(IndexRequestError, match="cannot approve own request"):
        svc.approve(item["id"], admin)
    approver = _principal(9, "other", {"read", "index:approve"})
    queued = svc.approve(item["id"], approver)
    assert queued["status"] == "queued"


def test_reject_cancel_and_illegal_transitions(conn, monkeypatch, tmp_path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    svc = _svc(conn, settings)
    actor = _principal(1, "dev", {"read", "index:request"})
    approver = _principal(2, "kb", {"read", "index:approve"})
    first = asyncio.run(svc.submit(_repo_payload(), actor))
    second = asyncio.run(svc.submit(_docs_payload(), actor))
    assert svc.reject(first["id"], "too broad", approver)["status"] == "rejected"
    assert svc.cancel(second["id"], actor)["status"] == "cancelled"
    with pytest.raises(IndexRequestError, match="cannot approve request in rejected"):
        svc.approve(first["id"], approver)
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "source",
    [
        "/tmp/local-repo",
        "file:///tmp/repo",
        "git://github.com/org/repo",
        "ssh://git@host/repo",
    ],
)
def test_remote_repo_source_rejects_non_http(
    conn, monkeypatch, tmp_path, source
) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    svc = _svc(conn, settings)
    actor = _principal(1, "dev", {"read", "index:request"})
    with pytest.raises(IndexRequestError):
        asyncio.run(svc.submit(_repo_payload(source), actor))


def test_wait_is_bounded_for_pending_request(conn, monkeypatch, tmp_path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    svc = _svc(conn, settings)
    actor = _principal(1, "dev", {"read", "index:request"})
    item = asyncio.run(svc.submit(_docs_payload(), actor))
    waited = asyncio.run(svc.wait(item["id"], actor, timeout=0.05, poll=0.01))
    assert waited["status"] == "pending"


def test_runner_writes_terminal_state(conn, monkeypatch, tmp_path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    jobs = JobManager(conn)
    svc = _svc(conn, settings, jobs=jobs)
    admin = _principal(1, "sys", {"read", "index:request", "ingest:write"})
    item = asyncio.run(svc.submit(_repo_payload(), admin))
    job = jobs.claim_next()
    assert job is not None
    runner = build_index_request_runner(
        settings, conn, lambda _job: {"ok": True}, jobs=jobs
    )
    assert asyncio.run(runner(job)) == {"ok": True}
    assert svc.get(item["id"], admin)["status"] == "done"

    failed_item = asyncio.run(
        svc.submit(_docs_payload(url="https://docs.example.com/fail"), admin)
    )
    failed_job = jobs.claim_next()
    assert failed_job is not None
    assert failed_job.params["index_request_id"] == failed_item["id"]
    failing = build_index_request_runner(
        settings,
        conn,
        lambda _job: (_ for _ in ()).throw(RuntimeError("boom")),
        jobs=jobs,
    )
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(failing(failed_job))
    assert svc.get(failed_item["id"], admin)["status"] == "failed"


def test_job_params_carry_request_id(conn, monkeypatch, tmp_path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    jobs = JobManager(conn)
    svc = _svc(conn, settings, jobs=jobs)
    admin = _principal(1, "sys", {"read", "index:request", "ingest:write"})
    item = asyncio.run(svc.submit(_docs_payload(), admin))
    job = jobs.get(int(item["job_id"]))
    assert job is not None
    assert job.params["index_request_id"] == item["id"]
    assert item["params"]["index_request_id"] == item["id"]
