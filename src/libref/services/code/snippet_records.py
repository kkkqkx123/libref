"""Snippet-level language evidence, parse outcomes and manual annotation storage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from libref.astminer.models import SNIPPET_STATUSES
from libref.services.infra.tenant import is_global, table_has_tenant

_RECORD_COLUMNS = (
    "version_id",
    "source_key",
    "snippet_id",
    "code_hash",
    "code",
    "url",
    "heading",
    "ordinal",
    "origin_kind",
    "origin_location",
    "origin_start_byte",
    "origin_end_byte",
    "origin_start_line",
    "origin_end_line",
    "raw_language",
    "language",
    "language_source",
    "language_rule",
    "parse_status",
    "attempts",
    "source_id",
    "tenant_id",
)


def _rows(cur) -> list[dict[str, Any]]:
    names = [str(item[0]) for item in (cur.description or ())]
    return [dict(zip(names, row, strict=False)) for row in cur.fetchall()]


def _row(cur):
    rows = _rows(cur)
    return rows[0] if rows else None


def _tenant_filter(conn, table: str, tenant_id: str) -> tuple[str, tuple[Any, ...]]:
    if table_has_tenant(conn, table) and not is_global(str(tenant_id)):
        return f" AND {table}.tenant_id=?", (str(tenant_id),)
    return "", ()


def record_source_options(
    conn,
    version_id: int,
    *,
    candidates: list[str] | tuple[str, ...] = (),
    rules: dict[str, str] | None = None,
    tenant_id: str = "default",
) -> None:
    """Persist the acquisition-time language facts of one documentation version."""
    clause, tenant = _tenant_filter(conn, "doc_snippet_sources", tenant_id)
    key = [int(version_id)] + list(tenant)
    conn.execute(
        "DELETE FROM doc_snippet_sources WHERE version_id=?" + clause, tuple(key)
    )
    conn.execute(
        "INSERT INTO doc_snippet_sources(tenant_id, version_id, snippet_languages,"
        " language_rules) VALUES(?, ?, ?, ?)",
        (
            str(tenant_id),
            int(version_id),
            json.dumps(list(candidates), ensure_ascii=False),
            json.dumps(dict(rules or {}), ensure_ascii=False, sort_keys=True),
        ),
    )


def load_source_options(
    conn, version_id: int, tenant_id: str = "default"
) -> dict[str, Any]:
    """Read the persisted acquisition facts for incremental snippet rebuilds."""
    clause, tenant = _tenant_filter(conn, "doc_snippet_sources", tenant_id)
    row = _row(
        conn.execute(
            "SELECT snippet_languages, language_rules FROM doc_snippet_sources"
            " WHERE version_id=?" + clause,
            tuple([int(version_id)] + list(tenant)),
        )
    )
    if row is None:
        return {"candidates": [], "rules": {}}
    return {
        "candidates": list(json.loads(row["snippet_languages"] or "[]")),
        "rules": dict(json.loads(row["language_rules"] or "{}")),
    }


def load_confirmed_languages(
    conn, version_id: int, source_key: str, tenant_id: str = "default"
) -> dict[str, str]:
    """Manual confirmations become explicit evidence on the next rebuild."""
    clause, tenant = _tenant_filter(conn, "doc_snippet_records", tenant_id)
    rows = _rows(
        conn.execute(
            "SELECT snippet_id, annotated_language FROM doc_snippet_records"
            " WHERE version_id=? AND source_key=? AND review_status='confirmed'"
            " AND annotated_language<>''" + clause,
            tuple([int(version_id), source_key] + list(tenant)),
        )
    )
    return {str(row["snippet_id"]): str(row["annotated_language"]) for row in rows}


def upsert_snippet_record(conn, record: dict[str, Any]) -> None:
    """Insert or update one snippet without multiplying rows on evidence reuse."""
    values = [record.get(name) for name in _RECORD_COLUMNS]
    updates = ", ".join(
        f"{name}=excluded.{name}"
        for name in _RECORD_COLUMNS
        if name not in ("version_id", "source_key", "snippet_id", "tenant_id")
    )
    marks = ",".join("?" for _ in values)
    conn.execute(
        f"INSERT INTO doc_snippet_records({', '.join(_RECORD_COLUMNS)})"
        f" VALUES({marks}) ON CONFLICT(version_id, source_key, snippet_id, tenant_id)"
        f" DO UPDATE SET {updates}",
        tuple(values),
    )


def list_snippet_records(
    conn,
    version_id: int,
    *,
    status: str = "",
    tenant_id: str = "default",
    limit: int = 500,
) -> list[dict[str, Any]]:
    """List snippet records by parse status for review and reporting."""
    if status and status not in SNIPPET_STATUSES:
        raise ValueError(f"unknown snippet status: {status}")
    clause, tenant = _tenant_filter(conn, "doc_snippet_records", tenant_id)
    sql = "SELECT * FROM doc_snippet_records WHERE version_id=?"
    params: list[Any] = [int(version_id)]
    if status:
        sql += " AND parse_status=?"
        params.append(status)
    sql += clause + " ORDER BY id LIMIT ?"
    params.extend(tenant)
    params.append(int(limit))
    return _rows(conn.execute(sql, tuple(params)))


def annotate_snippet_record(
    conn,
    version_id: int,
    source_key: str,
    snippet_id: str,
    *,
    language: str,
    reviewer: str,
    tenant_id: str = "default",
) -> dict[str, Any] | None:
    """Write a human-confirmed language back as explicit future evidence."""
    clause, tenant = _tenant_filter(conn, "doc_snippet_records", tenant_id)
    row = _row(
        conn.execute(
            "SELECT * FROM doc_snippet_records WHERE version_id=? AND source_key=?"
            " AND snippet_id=?" + clause,
            tuple([int(version_id), source_key, snippet_id] + list(tenant)),
        )
    )
    if row is None:
        return None
    conn.execute(
        "UPDATE doc_snippet_records SET review_status='confirmed',"
        " annotated_language=?, annotated_by=?, language=?,"
        " language_source='manual_review', annotated_at=?"
        " WHERE id=?",
        (
            str(language),
            str(reviewer),
            str(language),
            datetime.now(UTC).isoformat(timespec="seconds"),
            int(row["id"]),
        ),
    )
    conn.commit()
    updated = _row(
        conn.execute("SELECT * FROM doc_snippet_records WHERE id=?", (int(row["id"]),))
    )
    return updated
