"""PostgreSQL native full-text backend over the metadata session.

Search runs `websearch_to_tsquery` against the weighted `content_tsv` generated
column (title A, content B, path C) with `ts_rank_cd`; version and tenant
filters are pushed down as SQL predicates. Scores follow the contract's
ascending-best-first convention, so the rank value is negated. Content bodies
are never returned here; readers go back to the metadata session.
"""

from __future__ import annotations

from typing import Any


def _rank_expr(weights: tuple[float, float, float, float]) -> str:
    # ts_rank_cd signature is (weights float4[], tsv, query, normalization);
    # contract weight order (content, title, url/path, extra) maps to
    # (B, A, C, D), so the array is (A, B, C, D). Literals need an explicit
    # real[] cast or PostgreSQL infers numeric[] and the call fails.
    a, b, c, d = weights[0], weights[1], weights[2], 0.1
    return (
        "ts_rank_cd("
        f"ARRAY[{a}::real, {b}::real, {c}::real, {d}::real], "
        "content_tsv, query, 32)"
    )


def _tenant_active(tenant_id: str | None) -> bool:
    return tenant_id not in (None, "", "*")


class PostgresFulltextStore:
    """Shared full-text backend: PostgreSQL native text search."""

    def __init__(self, session) -> None:
        self._session = session

    def search(
        self,
        query: str,
        *,
        version_ids: list[int],
        weights: tuple[float, float, float, float],
        budget: int,
        tenant_id: str | None = None,
    ) -> list[tuple[int, float]]:
        q = (query or "").strip()
        if not q or budget <= 0 or not version_ids:
            return []
        marks = ",".join("%s" for _ in version_ids)
        params: list[Any] = [q, *version_ids]
        sql = (
            "SELECT id, -(" + _rank_expr(weights) + ") AS neg_rank"
            " FROM documents, websearch_to_tsquery('english', %s) query"
            f" WHERE content_tsv @@ query AND version_id IN ({marks})"
        )
        if _tenant_active(tenant_id):
            sql += " AND tenant_id = %s"
            params.append(tenant_id)
        sql += " ORDER BY neg_rank ASC LIMIT %s"
        params.append(budget)
        rows = self._session.execute(sql, tuple(params)).fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]

    def delete_version(self, version_id: int, tenant_id: str | None = None) -> int:
        # The tsvector is a generated column: deleting the document row removes
        # the index entry, so this only reports how many rows the delete touched.
        sql = "DELETE FROM documents WHERE version_id = %s"
        params: list[Any] = [version_id]
        if _tenant_active(tenant_id):
            sql += " AND tenant_id = %s"
            params.append(tenant_id)
        return int(self._session.execute(sql, tuple(params)).rowcount)

    def count(self, version_id: int, tenant_id: str | None = None) -> int:
        sql = "SELECT count(*) FROM documents WHERE version_id = %s"
        params: list[Any] = [version_id]
        if _tenant_active(tenant_id):
            sql += " AND tenant_id = %s"
            params.append(tenant_id)
        row = self._session.execute(sql, tuple(params)).fetchone()
        return int(row[0]) if row else 0

    def flush(self) -> None:
        # Generated-column maintenance is immediate; nothing to flush.
        return None

    def diagnostics(self) -> dict[str, Any]:
        info: dict[str, Any] = {"backend": "postgres-fulltext"}
        try:
            row = self._session.execute(
                "SELECT count(*) FROM documents WHERE content_tsv IS NOT NULL"
            ).fetchone()
            info["indexed_documents"] = int(row[0]) if row else 0
            info["reachable"] = True
        except Exception as exc:  # noqa: BLE001 — diagnostics must not raise
            info["reachable"] = False
            info["error"] = str(exc)
        return info
