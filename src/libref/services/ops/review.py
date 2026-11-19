"""Review workflow: pending list ranked by confidence; tenant-aware listing."""

from __future__ import annotations

import json
from typing import Any

from libref.services.infra.timestamps import iso_utc


class ReviewService:
    """Review orchestration; entity identity uses review_status rows for locating."""

    def review_map_for_version(self, version_id: int) -> dict[int, str]:
        rows = self._conn.execute(
            "SELECT ci.id, rs.status FROM call_instances ci "
            "JOIN review_status rs ON rs.entity_type='call_instance' AND rs.entity_id=ci.id "
            "WHERE ci.version_id=?",
            (version_id,),
        ).fetchall()
        return {int(r[0]): str(r[1]) for r in rows}

    def __init__(self, conn) -> None:
        self._conn = conn

    def _has_tenant(self) -> bool:
        try:
            cols = {
                row[1]
                for row in self._conn.execute(
                    "PRAGMA table_info(review_status)"
                ).fetchall()
            }
        except Exception:  # noqa: BLE001 — without table info, run unfiltered
            return False
        return "tenant_id" in cols

    def list_pending(
        self,
        limit: int = 50,
        *,
        entity_type: str = "call_instance",
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List pending items inside one tenant when the column exists."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        if entity_type != "call_instance":
            raise ValueError(f"unsupported entity_type: {entity_type}")
        use_tenant = tenant_id not in (None, "*") and self._has_tenant()
        if use_tenant:
            rows = self._conn.execute(
                "SELECT rs.entity_id, rs.status, rs.updated_at, ci.callee_name,"
                " ci.caller_file, ci.caller, ci.confidence, ci.args,"
                " ci.line_start, ci.line_end"
                " FROM review_status rs"
                " JOIN call_instances ci ON ci.id = rs.entity_id"
                " WHERE rs.entity_type='call_instance' AND rs.status='pending'"
                " AND rs.tenant_id=?"
                " ORDER BY CASE ci.confidence"
                " WHEN 'AMBIGUOUS' THEN 0 WHEN 'INFERRED' THEN 1"
                " WHEN 'EXTRACTED' THEN 2 ELSE 9 END,"
                " rs.updated_at, rs.entity_id LIMIT ?",
                (tenant_id, max(1, int(limit))),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT rs.entity_id, rs.status, rs.updated_at, ci.callee_name,"
                " ci.caller_file, ci.caller, ci.confidence, ci.args,"
                " ci.line_start, ci.line_end"
                " FROM review_status rs"
                " JOIN call_instances ci ON ci.id = rs.entity_id"
                " WHERE rs.entity_type='call_instance' AND rs.status='pending'"
                " ORDER BY CASE ci.confidence"
                " WHEN 'AMBIGUOUS' THEN 0 WHEN 'INFERRED' THEN 1"
                " WHEN 'EXTRACTED' THEN 2 ELSE 9 END,"
                " rs.updated_at, rs.entity_id LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        out = []
        for r in rows:
            raw = r[7]
            try:
                parsed = json.loads(raw) if raw else []
            except ValueError:
                parsed = [raw]
            out.append(
                {
                    "entity_id": int(r[0]),
                    "status": r[1],
                    "updated_at": iso_utc(r[2]),
                    "callee": r[3],
                    "caller_file": r[4],
                    "caller": r[5],
                    "confidence": r[6],
                    "args": parsed,
                    "line_start": r[8],
                    "line_end": r[9],
                }
            )
        return out

    def _row_tenant(self, entity_id: int) -> str | None:
        if not self._has_tenant():
            return None
        row = self._conn.execute(
            "SELECT tenant_id FROM review_status"
            " WHERE entity_type='call_instance' AND entity_id=?",
            (entity_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row[0])

    def _require_ownership(self, entity_id: int, tenant_id: str | None) -> None:
        if tenant_id is None or tenant_id == "*":
            return
        owner = self._row_tenant(entity_id)
        if owner is not None and owner != tenant_id:
            raise ValueError(f"unknown review: {entity_id}")

    def approve(
        self,
        entity_id: int,
        *,
        entity_type: str = "call_instance",
        tenant_id: str | None = None,
        principal_id: int | None = None,
    ) -> None:
        """Pending to approved; non-pending is rejected."""
        self._require_ownership(entity_id, tenant_id)
        self._transition(entity_id, "approved", entity_type=entity_type)

    def mark_conflict(
        self,
        entity_id: int,
        *,
        entity_type: str = "call_instance",
        tenant_id: str | None = None,
        principal_id: int | None = None,
    ) -> None:
        """Pending to conflict; non-pending is rejected."""
        self._require_ownership(entity_id, tenant_id)
        self._transition(entity_id, "conflict", entity_type=entity_type)

    def reopen(
        self,
        entity_id: int,
        *,
        entity_type: str = "call_instance",
        tenant_id: str | None = None,
        principal_id: int | None = None,
    ) -> None:
        """Reopen conflict to pending; non-conflict is rejected."""
        if entity_type != "call_instance":
            raise ValueError(f"unsupported entity_type: {entity_type}")
        self._require_ownership(entity_id, tenant_id)
        cur = self._conn.execute(
            "UPDATE review_status SET status='pending', updated_at=datetime('now')"
            " WHERE entity_type='call_instance' AND entity_id=? AND status='conflict'",
            (entity_id,),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"cannot reopen review row: {entity_id}")

    def _transition(self, entity_id: int, target: str, *, entity_type: str) -> None:
        if entity_type != "call_instance":
            raise ValueError(f"unsupported entity_type: {entity_type}")
        cur = self._conn.execute(
            "UPDATE review_status SET status=?, updated_at=datetime('now')"
            " WHERE entity_type='call_instance' AND entity_id=? AND status='pending'",
            (target, entity_id),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"cannot transition review row to {target}: {entity_id}")

    def apply(
        self,
        action: str,
        entity_id: int | None = None,
        limit: int = 50,
        *,
        tenant_id: str | None = None,
        principal_id: int | None = None,
    ) -> Any:
        """Unified review entry; tenant filtering applies to listing only."""
        if action == "list":
            return self.list_pending(limit=limit, tenant_id=tenant_id)
        if entity_id is None:
            raise ValueError(f"{action} requires entity_id")
        if action == "approve":
            self.approve(entity_id, tenant_id=tenant_id, principal_id=principal_id)
        elif action == "conflict":
            self.mark_conflict(
                entity_id, tenant_id=tenant_id, principal_id=principal_id
            )
        elif action == "reopen":
            self.reopen(entity_id, tenant_id=tenant_id, principal_id=principal_id)
        else:
            raise ValueError(f"unsupported review action: {action}")
        return {"ok": True}
