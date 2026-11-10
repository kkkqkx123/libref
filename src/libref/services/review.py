"""Review workflow: pending list ranked by confidence; terminal transitions explicitly reject illegal moves."""

from __future__ import annotations

import json
from typing import Any

_CONFIDENCE_RANK = {"AMBIGUOUS": 0, "INFERRED": 1, "EXTRACTED": 2}


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

    def list_pending(
        self, limit: int = 50, *, entity_type: str = "call_instance"
    ) -> list[dict[str, Any]]:
        """List pending items, lowest confidence first, then by update time."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        if entity_type != "call_instance":
            raise ValueError(f"unsupported entity_type: {entity_type}")
        rows = self._conn.execute(
            "SELECT rs.entity_id, rs.status, rs.updated_at, ci.callee_name,"
            " ci.caller_file, ci.caller, ci.confidence, ci.args,"
            " ci.line_start, ci.line_end"
            " FROM review_status rs"
            " JOIN call_instances ci ON ci.id = rs.entity_id"
            " WHERE rs.entity_type='call_instance' AND rs.status='pending'"
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
                    "updated_at": r[2],
                    "callee": r[3],
                    "caller_file": r[4],
                    "caller": r[5],
                    "confidence": r[6],
                    "args": parsed,
                    "line_start": r[8],
                    "line_end": r[9],
                }
            )
        out.sort(
            key=lambda r: (
                _CONFIDENCE_RANK.get(r["confidence"], 9),
                r["updated_at"] or "",
                r["entity_id"],
            )
        )
        return out[:limit]

    def approve(self, entity_id: int, *, entity_type: str = "call_instance") -> None:
        """Pending to approved; non-pending is rejected."""
        self._transition(entity_id, "approved", entity_type=entity_type)

    def mark_conflict(
        self, entity_id: int, *, entity_type: str = "call_instance"
    ) -> None:
        """Pending to conflict; non-pending is rejected."""
        self._transition(entity_id, "conflict", entity_type=entity_type)

    def reopen(self, entity_id: int, *, entity_type: str = "call_instance") -> None:
        """Reopen conflict to pending; non-conflict is rejected."""
        if entity_type != "call_instance":
            raise ValueError(f"unsupported entity_type: {entity_type}")
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

    def apply(self, action: str, entity_id: int | None = None, limit: int = 50) -> Any:
        """Unified review: list pending items or transition one item."""
        if action == "list":
            return self.list_pending(limit=limit)
        if entity_id is None:
            raise ValueError(f"{action} requires entity_id")
        if action == "approve":
            self.approve(entity_id)
        elif action == "conflict":
            self.mark_conflict(entity_id)
        elif action == "reopen":
            self.reopen(entity_id)
        else:
            raise ValueError(f"unsupported review action: {action}")
        return {"ok": True}
