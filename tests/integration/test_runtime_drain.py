"""Multi-replica runtime tests: graceful drain and configuration agreement."""

from __future__ import annotations

import threading

import pytest

from libref.db.sqlite_session import SqliteSession
from libref.services.infra.jobs import JobManager
from libref.services.infra.runtime import (
    CONFIG_FINGERPRINT_KEY,
    check_config_fingerprint,
    drain,
    is_draining,
)


@pytest.fixture()
def session(tmp_path):
    s = SqliteSession.open(tmp_path / "rt.db", check_same_thread=False)
    s.apply_migrations()
    yield s
    s.close()


def test_drain_waits_for_running_jobs(session):
    manager = JobManager(session)
    job_id = manager.submit("noop")
    claimed = manager.claim_next()
    assert claimed is not None and claimed.id == job_id
    manager.stop_worker()

    # Simulate an in-flight job finishing mid-drain.
    timer = threading.Timer(0.2, lambda: manager._update(job_id, status="done"))
    timer.start()
    remaining = drain(manager, timeout=5.0, poll_interval=0.05)
    timer.join()
    assert remaining == 0
    assert is_draining() is True


def test_drain_reports_unfinished_on_timeout(session):
    manager = JobManager(session)
    manager.submit("stuck")
    assert manager.claim_next() is not None
    manager.stop_worker()
    remaining = drain(manager, timeout=0.3, poll_interval=0.05)
    assert remaining == 1


def test_fingerprint_agrees_and_refuses_on_divergence(session, monkeypatch, tmp_path):
    from libref.config import LibrefSettings

    settings_a = LibrefSettings(db_path=str(tmp_path / "a.db"))
    settings_b = LibrefSettings(
        db_path=str(tmp_path / "a.db"), web_port=settings_a.web_port + 1
    )

    assert check_config_fingerprint(session, settings_a) is True
    row = session.execute(
        "SELECT value FROM schema_meta WHERE key=?", (CONFIG_FINGERPRINT_KEY,)
    ).fetchone()
    assert row is not None
    # Same content agrees; different content refuses.
    assert check_config_fingerprint(session, settings_a) is True
    assert check_config_fingerprint(session, settings_b) is False
