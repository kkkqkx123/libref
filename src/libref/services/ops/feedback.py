"""Developer feedback records; intentionally separate from review transitions."""

from __future__ import annotations

from typing import Any

from libref.services.infra.tenant import GLOBAL_TENANT


class FeedbackService:
    """Append-only feedback with explicit resolve; never touches review rows."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def submit(
        self,
        *,
        tenant_id: str,
        principal_id: int | None,
        entity_type: str = "call_instance",
        entity_id: int = 0,
        kind: str = "general",
        body: str,
    ) -> int:
        """Record one feedback item and return its id."""
        text = (body or "").strip()
        if not text:
            raise ValueError("feedback body is empty")
        from libref.services.infra.auth import ensure_auth_tables

        ensure_auth_tables(self._conn)
        cur = self._conn.execute(
            "INSERT INTO feedback(tenant_id, principal_id, entity_type, entity_id,"
            " kind, body) VALUES(?,?,?,?,?,?)",
            (tenant_id, principal_id, entity_type, entity_id, kind, text),
        )
        self._conn.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def list_items(
        self, *, tenant_id: str, limit: int = 50, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List feedback inside one tenant, newest last for stable review order."""
        from libref.services.infra.auth import ensure_auth_tables

        ensure_auth_tables(self._conn)
        if tenant_id == GLOBAL_TENANT:
            if status is None:
                rows = self._conn.execute(
                    "SELECT id, tenant_id, principal_id, entity_type, entity_id,"
                    " kind, body, status, created_at, resolved_by, resolved_at"
                    " FROM feedback ORDER BY id LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, tenant_id, principal_id, entity_type, entity_id,"
                    " kind, body, status, created_at, resolved_by, resolved_at"
                    " FROM feedback WHERE status=? ORDER BY id LIMIT ?",
                    (status, limit),
                ).fetchall()
        elif status is None:
            rows = self._conn.execute(
                "SELECT id, tenant_id, principal_id, entity_type, entity_id,"
                " kind, body, status, created_at, resolved_by, resolved_at"
                " FROM feedback WHERE tenant_id=? ORDER BY id LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, tenant_id, principal_id, entity_type, entity_id,"
                " kind, body, status, created_at, resolved_by, resolved_at"
                " FROM feedback WHERE tenant_id=? AND status=? ORDER BY id LIMIT ?",
                (tenant_id, status, limit),
            ).fetchall()
        return [
            {
                "id": int(row[0]),
                "tenant_id": str(row[1]),
                "principal_id": row[2],
                "entity_type": str(row[3]),
                "entity_id": int(row[4]),
                "kind": str(row[5]),
                "body": str(row[6]),
                "status": str(row[7]),
                "created_at": str(row[8]),
                "resolved_by": row[9],
                "resolved_at": row[10],
            }
            for row in rows
        ]

    def resolve(
        self,
        feedback_id: int,
        *,
        resolved_by: int | None,
        tenant_id: str | None = None,
    ) -> None:
        """Mark one item resolved inside its tenant; repeat resolution is rejected."""
        from libref.services.infra.auth import ensure_auth_tables

        ensure_auth_tables(self._conn)
        sql = (
            "UPDATE feedback SET status='resolved', resolved_by=?,"
            " resolved_at=datetime('now')"
            " WHERE id=? AND status != 'resolved'"
        )
        params: list[Any] = [resolved_by, feedback_id]
        if tenant_id not in (None, GLOBAL_TENANT):
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"unknown or resolved feedback: {feedback_id}")
