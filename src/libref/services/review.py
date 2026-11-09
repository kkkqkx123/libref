"""审核工作流：待定列表按置信优先级，终态流转显式拒绝非法转移。"""

from __future__ import annotations

from typing import Any

_CONFIDENCE_RANK = {"AMBIGUOUS": 0, "INFERRED": 1, "EXTRACTED": 2}


class ReviewService:
    """审核编排，实体标识沿用 review_status 行定位。"""

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
        """列出待定项，置信越低越靠前，再按更新时间。"""
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
        out = [
            {
                "entity_id": int(r[0]),
                "status": r[1],
                "updated_at": r[2],
                "callee": r[3],
                "caller_file": r[4],
                "caller": r[5],
                "confidence": r[6],
                "args": r[7],
                "line_start": r[8],
                "line_end": r[9],
            }
            for r in rows
        ]
        out.sort(
            key=lambda r: (
                _CONFIDENCE_RANK.get(r["confidence"], 9),
                r["updated_at"] or "",
                r["entity_id"],
            )
        )
        return out[:limit]

    def approve(self, entity_id: int, *, entity_type: str = "call_instance") -> None:
        """待定转已核准，非待定拒绝。"""
        self._transition(entity_id, "approved", entity_type=entity_type)

    def mark_conflict(self, entity_id: int, *, entity_type: str = "call_instance") -> None:
        """待定转冲突，非待定拒绝。"""
        self._transition(entity_id, "conflict", entity_type=entity_type)

    def reopen(self, entity_id: int, *, entity_type: str = "call_instance") -> None:
        """冲突重开为待定，非冲突拒绝。"""
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

    def _transition(
        self, entity_id: int, target: str, *, entity_type: str
    ) -> None:
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
