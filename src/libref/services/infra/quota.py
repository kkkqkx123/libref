"""Per-subject quota governance: effective-limit resolution, atomic usage upserts, and over-limit decisions.

Resources are counted per (tenant, principal, resource, period). Principal ``0`` is the
tenant-default subject for policies that lack a specific principal. Resolution order is
principal policy, then tenant default policy, then the global config fallback fields.
Database failures fail open (allow) so quota bookkeeping never becomes an outage.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from libref.db.contract import MetadataSession
from libref.db.schema import QUOTA_RUNTIME_SQL

RESOURCES = frozenset(
    {
        "http_requests",
        "llm_prompt_tokens",
        "llm_completion_tokens",
        "scrape_pages",
        "job_submissions",
        "snapshot_bytes",
        "repos_bytes",
    }
)

# Global settings field + window type used as the final fallback per resource.
# Storage resources are absent: their global floors are snapshot_quota_mb / repos_quota_mb.
_GLOBAL_FALLBACK: dict[str, tuple[str, str]] = {
    "http_requests": ("quota_http_requests_per_hour", "hour"),
    "llm_prompt_tokens": ("quota_llm_tokens_per_day", "day"),
    "llm_completion_tokens": ("quota_llm_tokens_per_day", "day"),
    "job_submissions": ("quota_job_submissions_per_hour", "hour"),
}

TENANT_DEFAULT_SUBJECT = 0
_POLICY_CACHE_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class QuotaDecision:
    """Outcome of a quota check; ``limit_value`` 0 means unlimited."""

    allowed: bool
    resource: str
    limit_value: int
    used: int
    period_key: str
    reset_at: str


def ensure_quota_tables(conn: MetadataSession) -> None:
    """Create quota tables when absent; idempotent for fresh test databases."""
    conn.executescript(QUOTA_RUNTIME_SQL)
    conn.commit()


def period_key(window_type: str, now: float | None = None) -> str:
    """UTC period bucket for a window type: hour/day/month."""
    import datetime as _dt

    moment = _dt.datetime.fromtimestamp(time.time() if now is None else now, _dt.UTC)
    if window_type == "day":
        return moment.strftime("%Y-%m-%d")
    if window_type == "month":
        return moment.strftime("%Y-%m")
    return moment.strftime("%Y-%m-%dT%H")


def reset_at(window_type: str, now: float | None = None) -> str:
    """ISO UTC timestamp of the next period boundary."""
    import datetime as _dt

    moment = _dt.datetime.fromtimestamp(time.time() if now is None else now, _dt.UTC)
    if window_type == "day":
        boundary = (moment + _dt.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif window_type == "month":
        year, month = moment.year, moment.month
        nxt_year, nxt_month = (year + 1, 1) if month == 12 else (year, month + 1)
        boundary = _dt.datetime(nxt_year, nxt_month, 1, tzinfo=_dt.UTC)
    else:
        boundary = (moment + _dt.timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )
    return boundary.strftime("%Y-%m-%dT%H:%M:%SZ")


def _subject(principal_id: int | None) -> int:
    return TENANT_DEFAULT_SUBJECT if principal_id is None else int(principal_id)


def set_policy(
    conn: MetadataSession,
    *,
    tenant_id: str,
    resource: str,
    limit_value: int,
    window_type: str = "hour",
    principal_id: int | None = None,
) -> None:
    """Upsert one quota policy row; ``limit_value`` 0 means unlimited."""
    if resource not in RESOURCES:
        raise ValueError(f"unknown quota resource: {resource}")
    if window_type not in ("hour", "day", "month"):
        raise ValueError(f"unknown quota window: {window_type}")
    ensure_quota_tables(conn)
    conn.execute(
        "INSERT INTO quota_policies(tenant_id, principal_id, resource, limit_value,"
        " window_type, updated_at) VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(tenant_id, principal_id, resource, window_type)"
        " DO UPDATE SET limit_value=excluded.limit_value,"
        " updated_at=excluded.updated_at",
        (
            tenant_id,
            _subject(principal_id),
            resource,
            int(limit_value),
            window_type,
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ),
    )
    conn.commit()


def _db_policy(
    conn: MetadataSession, *, tenant_id: str, subject: int, resource: str
) -> tuple[int, str] | None:
    row = conn.execute(
        "SELECT limit_value, window_type FROM quota_policies"
        " WHERE tenant_id=? AND principal_id=? AND resource=?"
        " ORDER BY principal_id=? DESC LIMIT 1",
        (tenant_id, subject, resource, TENANT_DEFAULT_SUBJECT),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), str(row[1])


def _config_fallback(resource: str, settings) -> tuple[int, str] | None:
    mapping = _GLOBAL_FALLBACK.get(resource)
    if mapping is None or settings is None:
        return None
    field, window = mapping
    value = getattr(settings, field, 0)
    return int(value or 0), window


_policy_cache: dict[tuple[str, int, str], tuple[float, int, str]] = {}
_policy_cache_lock = threading.Lock()


def effective_policy(
    conn: MetadataSession,
    *,
    tenant_id: str,
    principal_id: int | None,
    resource: str,
    settings=None,
) -> tuple[int, str]:
    """Resolve the effective (limit_value, window_type); limit 0 = unlimited."""
    subject = _subject(principal_id)
    cache_key = (tenant_id, subject, resource)
    now = time.monotonic()
    with _policy_cache_lock:
        cached = _policy_cache.get(cache_key)
        if cached is not None and cached[0] > now:
            return cached[1], cached[2]
    try:
        found = _db_policy(
            conn, tenant_id=tenant_id, subject=subject, resource=resource
        )
        if found is None and subject != TENANT_DEFAULT_SUBJECT:
            found = _db_policy(
                conn,
                tenant_id=tenant_id,
                subject=TENANT_DEFAULT_SUBJECT,
                resource=resource,
            )
        if found is None:
            found = _config_fallback(resource, settings)
    except Exception:  # noqa: BLE001 — quota reads fail open to the config fallback
        found = _config_fallback(resource, settings)
    if found is None:
        limit, window = 0, "hour"
    else:
        limit, window = found
    with _policy_cache_lock:
        _policy_cache[cache_key] = (now + _POLICY_CACHE_TTL_SECONDS, limit, window)
    return limit, window


def invalidate_policy_cache() -> None:
    """Drop cached policies after an admin writes new ones."""
    with _policy_cache_lock:
        _policy_cache.clear()


def current_usage(
    conn: MetadataSession,
    *,
    tenant_id: str,
    principal_id: int | None,
    resource: str,
    key: str,
) -> int:
    """Read the usage counter for one period bucket."""
    subject = _subject(principal_id)
    row = conn.execute(
        "SELECT used FROM usage_counters WHERE tenant_id=? AND principal_id=?"
        " AND resource=? AND period_key=?",
        (tenant_id, subject, resource, key),
    ).fetchone()
    return int(row[0]) if row else 0


def add_usage(
    conn: MetadataSession,
    *,
    tenant_id: str,
    principal_id: int | None,
    resource: str,
    amount: int = 1,
    key: str | None = None,
    window_type: str = "hour",
) -> int:
    """Atomically accumulate usage for one period bucket; returns the new total."""
    bucket = key if key is not None else period_key(window_type)
    subject = _subject(principal_id)
    conn.execute(
        "INSERT INTO usage_counters(tenant_id, principal_id, resource, period_key, used,"
        " updated_at) VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(tenant_id, principal_id, resource, period_key)"
        " DO UPDATE SET used=used+excluded.used, updated_at=excluded.updated_at",
        (
            tenant_id,
            subject,
            resource,
            bucket,
            int(amount),
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ),
    )
    conn.commit()
    return current_usage(
        conn,
        tenant_id=tenant_id,
        principal_id=principal_id,
        resource=resource,
        key=bucket,
    )


def check(
    conn: MetadataSession,
    *,
    tenant_id: str,
    principal_id: int | None,
    resource: str,
    cost: int = 1,
    settings=None,
) -> QuotaDecision:
    """Decide allow/deny for one charge against the effective policy."""
    limit, window = effective_policy(
        conn,
        tenant_id=tenant_id,
        principal_id=principal_id,
        resource=resource,
        settings=settings,
    )
    key = period_key(window)
    try:
        used = current_usage(
            conn,
            tenant_id=tenant_id,
            principal_id=principal_id,
            resource=resource,
            key=key,
        )
    except Exception:  # noqa: BLE001 — DB failure fails open with an unlimited decision
        used = 0
        limit = 0
    return QuotaDecision(
        allowed=limit <= 0 or used + int(cost) <= limit,
        resource=resource,
        limit_value=limit,
        used=used,
        period_key=key,
        reset_at=reset_at(window),
    )


def enforce(
    conn: MetadataSession,
    *,
    tenant_id: str,
    principal_id: int | None,
    resource: str,
    cost: int = 1,
    settings=None,
    request_id: str = "",
) -> QuotaDecision | None:
    """Charge one request; return the denial decision or None when allowed."""
    from libref.services.infra.tenant import owner_tenant

    tenant_id = owner_tenant(tenant_id)
    decision = check(
        conn,
        tenant_id=tenant_id,
        principal_id=principal_id,
        resource=resource,
        cost=cost,
        settings=settings,
    )
    if decision.allowed:
        try:
            add_usage(
                conn,
                tenant_id=tenant_id,
                principal_id=principal_id,
                resource=resource,
                amount=cost,
                key=decision.period_key,
            )
        except Exception:  # noqa: BLE001, S110 — bookkeeping never blocks serving
            pass
        return None
    record_event(
        conn,
        tenant_id=tenant_id,
        principal_id=principal_id,
        resource=resource,
        result="denied",
        used=decision.used,
        limit_value=decision.limit_value,
        request_id=request_id,
    )
    from libref.services.ops.audit import write_audit

    write_audit(
        conn,
        tenant_id=tenant_id,
        principal_id=principal_id,
        action="quota",
        target=resource,
        result="denied",
        request_id=request_id,
    )
    return decision


def record_event(
    conn: MetadataSession,
    *,
    tenant_id: str,
    principal_id: int | None,
    resource: str,
    result: str,
    used: int = 0,
    limit_value: int = 0,
    request_id: str = "",
) -> None:
    """Persist one deny/degrade decision for audit; best effort, never raises."""
    try:
        ensure_quota_tables(conn)
        conn.execute(
            "INSERT INTO quota_events(tenant_id, principal_id, resource, result, used,"
            " limit_value, request_id) VALUES(?,?,?,?,?,?,?)",
            (
                tenant_id,
                _subject(principal_id),
                resource,
                result,
                int(used),
                int(limit_value),
                request_id,
            ),
        )
        conn.commit()
    except Exception:  # noqa: BLE001, S110 — audit of quota events must not block serving
        pass
