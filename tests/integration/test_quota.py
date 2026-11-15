"""Per-subject quota service: policy resolution, atomic usage, and over-limit decisions."""

from __future__ import annotations

import sqlite3

import pytest

from libref.db import apply_migrations
from libref.services.infra import quota


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    apply_migrations(c)
    return c


class _Settings:
    quota_enabled = True
    quota_http_requests_per_hour = 0
    quota_llm_tokens_per_day = 0
    quota_job_submissions_per_hour = 0


def test_period_key_buckets(conn: sqlite3.Connection) -> None:
    hour_key = quota.period_key("hour", now=1731648000.0)  # 2024-11-15T05:00Z
    day_key = quota.period_key("day", now=1731648000.0)
    assert hour_key == "2024-11-15T05"
    assert day_key == "2024-11-15"
    assert len(quota.period_key("month", now=1731648000.0)) == 7


def test_add_usage_is_atomic_and_accumulating(conn: sqlite3.Connection) -> None:
    first = quota.add_usage(
        conn, tenant_id="t1", principal_id=7, resource="http_requests", amount=3
    )
    second = quota.add_usage(
        conn, tenant_id="t1", principal_id=7, resource="http_requests", amount=2
    )
    assert first == 3
    assert second == 5


def test_usage_isolated_by_subject_and_period(conn: sqlite3.Connection) -> None:
    quota.add_usage(
        conn, tenant_id="t1", principal_id=7, resource="http_requests", amount=4
    )
    quota.add_usage(
        conn,
        tenant_id="t1",
        principal_id=7,
        resource="http_requests",
        amount=1,
        key="2099-01-01T00",
    )
    assert (
        quota.current_usage(
            conn,
            tenant_id="t1",
            principal_id=8,
            resource="http_requests",
            key=quota.period_key("hour"),
        )
        == 0
    )
    assert (
        quota.current_usage(
            conn,
            tenant_id="t1",
            principal_id=7,
            resource="http_requests",
            key="2099-01-01T00",
        )
        == 1
    )


def test_policy_priority_principal_over_tenant(conn: sqlite3.Connection) -> None:
    quota.set_policy(
        conn,
        tenant_id="t1",
        resource="http_requests",
        limit_value=100,
        principal_id=None,
    )
    quota.set_policy(
        conn, tenant_id="t1", resource="http_requests", limit_value=5, principal_id=7
    )
    quota.invalidate_policy_cache()
    limit, _window = quota.effective_policy(
        conn,
        tenant_id="t1",
        principal_id=7,
        resource="http_requests",
        settings=_Settings(),
    )
    assert limit == 5
    limit, _window = quota.effective_policy(
        conn,
        tenant_id="t1",
        principal_id=8,
        resource="http_requests",
        settings=_Settings(),
    )
    assert limit == 100


def test_config_fallback_when_no_policy(conn: sqlite3.Connection) -> None:
    class _S(_Settings):
        quota_http_requests_per_hour = 42

    quota.invalidate_policy_cache()
    limit, window = quota.effective_policy(
        conn, tenant_id="t1", principal_id=7, resource="http_requests", settings=_S()
    )
    assert (limit, window) == (42, "hour")


def test_check_denies_over_limit(conn: sqlite3.Connection) -> None:
    quota.set_policy(
        conn,
        tenant_id="t1",
        resource="job_submissions",
        limit_value=3,
        principal_id=None,
    )
    quota.invalidate_policy_cache()
    for _ in range(3):
        decision = quota.check(
            conn,
            tenant_id="t1",
            principal_id=9,
            resource="job_submissions",
            settings=_Settings(),
        )
        assert decision.allowed
        quota.add_usage(
            conn, tenant_id="t1", principal_id=9, resource="job_submissions"
        )
    decision = quota.check(
        conn,
        tenant_id="t1",
        principal_id=9,
        resource="job_submissions",
        settings=_Settings(),
    )
    assert not decision.allowed
    assert decision.limit_value == 3
    assert decision.used == 3


def test_zero_limit_means_unlimited(conn: sqlite3.Connection) -> None:
    decision = quota.check(
        conn,
        tenant_id="t1",
        principal_id=None,
        resource="http_requests",
        settings=_Settings(),
    )
    assert decision.allowed
    assert decision.limit_value == 0


def test_record_event_persists_denial(conn: sqlite3.Connection) -> None:
    quota.record_event(
        conn,
        tenant_id="t1",
        principal_id=5,
        resource="http_requests",
        result="denied",
        used=10,
        limit_value=10,
        request_id="abc",
    )
    row = conn.execute(
        "SELECT tenant_id, principal_id, resource, result, request_id FROM quota_events"
    ).fetchone()
    assert row == ("t1", 5, "http_requests", "denied", "abc")


def test_record_event_never_raises(conn: sqlite3.Connection) -> None:
    conn.close()
    quota.record_event(
        conn,
        tenant_id="t",
        principal_id=None,
        resource="http_requests",
        result="denied",
    )
