"""Append-only audit trail for privileged writes."""

from __future__ import annotations

from typing import Any


def write_audit(
    conn: Any,
    *,
    tenant_id: str,
    principal_id: int | None,
    action: str,
    target: str = "",
    result: str = "ok",
    request_id: str = "",
) -> None:
    """Record one privileged action; audit writes never raise."""
    try:
        from libref.db.conn import write_lock as _guard
        from libref.services.infra.auth import ensure_auth_tables
        from libref.services.infra.tenant import owner_tenant

        ensure_auth_tables(conn)
        with _guard(conn):
            conn.execute(
                "INSERT INTO audit_log(tenant_id, principal_id, action, target,"
                " result, request_id) VALUES(?,?,?,?,?,?)",
                (
                    owner_tenant(tenant_id),
                    principal_id,
                    action,
                    target,
                    result,
                    request_id,
                ),
            )
            conn.commit()
    except Exception:  # noqa: BLE001 — audit must not break business
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001, S110 — best effort only
            pass
