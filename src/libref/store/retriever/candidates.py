"""FTS and vector candidate generation with version scoping."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from libref.text.embedding import strip_markdown_for_embedding
from libref.text.query import safe_fts_query
from libref.text.snippet import make_snippet


def _version_ids(
    conn: sqlite3.Connection,
    library: str | None,
    version: str | None,
    ecosystem: str | None,
    tenant_id: str | None = None,
) -> list[int]:
    from libref.services.infra.tenant import (
        is_global,
        resolve_version_id,
        table_has_tenant,
    )

    scoped = tenant_id not in (None, "*") and not is_global(str(tenant_id or ""))
    if scoped and not (
        table_has_tenant(conn, "libraries") or table_has_tenant(conn, "versions")
    ):
        scoped = False
    if scoped and library is not None and version is not None:
        try:
            vid = resolve_version_id(conn, str(tenant_id), library, version, ecosystem)
        except ValueError:
            return []
        return [int(vid)]
    filters = []
    params: list[Any] = []
    if library is not None:
        filters.append("l.name = ?")
        params.append(library)
    if ecosystem is not None:
        filters.append("l.ecosystem = ?")
        params.append(ecosystem)
    if version is not None:
        filters.append("v.version = ?")
        params.append(version)
    if scoped:
        assert tenant_id is not None
        if table_has_tenant(conn, "libraries"):
            filters.append("l.tenant_id = ?")
            params.append(str(tenant_id))
        if table_has_tenant(conn, "versions"):
            filters.append("v.tenant_id = ?")
            params.append(str(tenant_id))
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    # Newest ingested first: scoped recall fans out over versions until the cap truncates,
    # and the truncated tail must be the oldest versions rather than the freshest ones.
    rows = conn.execute(
        f"SELECT v.id FROM versions v JOIN libraries l ON l.id = v.library_id {where}"
        " ORDER BY v.id DESC",
        params,
    ).fetchall()
    return [int(r[0]) for r in rows]


def _bm25_expr(weights: tuple[float, float, float, float]) -> str:
    return (
        f"bm25(documents_fts, {weights[0]}, {weights[1]}, {weights[2]}, {weights[3]})"
    )


def fts_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    library: str | None = None,
    version: str | None = None,
    ecosystem: str | None = None,
    weights: tuple[float, float, float, float],
    budget: int,
    tenant_id: str | None = None,
    version_ids: list[int] | None = None,
) -> list[tuple[int, float]]:
    """Single shared full-text read path, returning document ids with raw scores.

    Scores are raw match values in ascending order, best first. Without filters
    the database sorts and truncates. With filters the scope resolves to version
    ids first and only scoped candidates are ranked, so large unrelated versions
    never enter the sort. An empty scope yields nothing. An explicit version_ids
    list bypasses library/version resolution and filters to those versions
    directly, matching the full-text contract surface.
    """
    safe = safe_fts_query(query)
    if not safe or budget <= 0:
        return []
    if version_ids is not None and not [v for v in version_ids]:
        return []
    from libref.services.infra.tenant import is_global as _is_global_t
    from libref.services.infra.tenant import table_has_tenant as _has_tenant_t

    _tenant_scoped = tenant_id not in (None, "*") and not _is_global_t(
        str(tenant_id or "")
    )
    if _tenant_scoped and not (
        _has_tenant_t(conn, "libraries")
        or _has_tenant_t(conn, "versions")
        or _has_tenant_t(conn, "documents")
    ):
        _tenant_scoped = False
    scoped = (
        library is not None
        or version is not None
        or ecosystem is not None
        or _tenant_scoped
        or version_ids is not None
    )
    if not scoped:
        rows = conn.execute(
            f"SELECT documents_fts.rowid, {_bm25_expr(weights)} FROM documents_fts "
            "WHERE documents_fts MATCH ? "
            f"ORDER BY {_bm25_expr(weights)} LIMIT ?",
            [safe, budget],
        ).fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]
    if version_ids is not None:
        vids = [int(v) for v in version_ids]
    else:
        vids = _version_ids(
            conn, library, version, ecosystem, tenant_id if _tenant_scoped else None
        )
    if not vids:
        return []
    marks = ",".join("?" for _ in vids)
    tenant_probe = ""
    tenant_params: list[Any] = []
    if _tenant_scoped and _has_tenant_t(conn, "documents"):
        tenant_probe = " AND d.tenant_id = ?"
        tenant_params = [str(tenant_id)]
    # The scope predicate is a correlated EXISTS rather than `rowid IN (subquery)`:
    # SQLite materialises an IN subquery into an unindexed list and probes it once per
    # index row, which turns a scoped search over a large corpus quadratic (seconds per
    # query instead of milliseconds). The EXISTS form is an indexed primary-key probe.
    rows = conn.execute(
        f"SELECT documents_fts.rowid, {_bm25_expr(weights)} FROM documents_fts "
        "WHERE documents_fts MATCH ? "
        "AND EXISTS (SELECT 1 FROM documents d WHERE d.id = documents_fts.rowid "
        f"AND d.version_id IN ({marks}){tenant_probe}) "
        f"ORDER BY {_bm25_expr(weights)} LIMIT ?",
        [safe, *vids, *tenant_params, budget],
    ).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows]


def _fts_candidates(
    conn: sqlite3.Connection,
    query: str,
    library: str | None,
    version: str | None,
    ecosystem: str | None,
    limit: int,
    *,
    weights: tuple[float, float, float, float],
    tenant_id: str | None = None,
) -> list[int]:
    return [
        doc_id
        for doc_id, _ in fts_search(
            conn,
            query,
            library=library,
            version=version,
            ecosystem=ecosystem,
            weights=weights,
            budget=limit,
            tenant_id=tenant_id,
        )
    ]


def _fetch_details(
    conn: sqlite3.Connection,
    doc_ids: list[int],
    query: str,
    *,
    snippet_radius: int,
    snippet_cap: int,
) -> dict[int, dict[str, Any]]:
    if not doc_ids:
        return {}
    marks = ",".join("?" for _ in doc_ids)
    rows = conn.execute(
        "SELECT d.id, d.url, d.content, json_extract(d.metadata, '$.title'), "
        "json_extract(d.metadata, '$.path'), l.name, v.version, d.version_id, "
        "d.metadata, d.page_id, d.sort_order "
        "FROM documents d "
        "JOIN versions v ON v.id = d.version_id "
        "JOIN libraries l ON l.id = d.library_id "
        f"WHERE d.id IN ({marks})",
        doc_ids,
    ).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        try:
            meta = json.loads(r[8]) if r[8] else {}
        except ValueError:
            meta = {}
        out[int(r[0])] = {
            "id": int(r[0]),
            "url": r[1],
            "snippet": make_snippet(
                strip_markdown_for_embedding(r[2]),
                query,
                radius=snippet_radius,
                cap=snippet_cap,
            ),
            "title": r[3],
            "path": r[4],
            "library": r[5],
            "version": r[6],
            "version_id": int(r[7]),
            "content": r[2] or "",
            "chunk_path": meta.get("chunk_path", "embedding"),
            "source_group_id": meta.get("source_group_id", ""),
            "chunk_id": meta.get("chunk_id", ""),
            "span_kind": meta.get("span_kind", "document_range"),
            "merged_from": list(meta.get("merged_from", []) or []),
            "overlap_from": meta.get("overlap_from"),
            "overlap_text": str(meta.get("overlap_text", "") or ""),
            "chunk_index": int(meta.get("chunk_index", 0) or 0),
            "total_chunks": max(1, int(meta.get("total_chunks", 1) or 1)),
            "keywords": list(meta.get("keywords", []) or []),
            "content_type": meta.get("content_type", "text/markdown"),
            "entities": list(meta.get("entities", []) or []),
            "page_id": int(r[9]),
            "sort_order": int(r[10]),
        }
    return out


def _profile_id_by_version(
    conn: sqlite3.Connection, version_ids: list[int]
) -> dict[int, int]:
    if not version_ids:
        return {}
    marks = ",".join("?" for _ in version_ids)
    rows = conn.execute(
        "SELECT version_id, id FROM mining_profiles"
        f" WHERE version_id IN ({marks}) AND name='usage'",
        version_ids,
    ).fetchall()
    return {int(r[0]): int(r[1]) for r in rows}


def _current_vids(conn, settings, vids: list[int]) -> tuple[list[int], bool]:
    """Split version ids into vector-searchable ones; drifted ones are skipped."""
    from libref.config import setting_or_default

    if not vids:
        return [], False
    model = setting_or_default(settings, "embedding_model")
    dim = int(setting_or_default(settings, "embedding_dim"))
    marks = ",".join("?" for _ in vids)
    known = {
        int(r[0]): (r[1], int(r[2]))
        for r in conn.execute(
            f"SELECT version_id, model, dim FROM vec_version_meta WHERE version_id IN ({marks})",
            [int(v) for v in vids],
        ).fetchall()
    }
    current: list[int] = []
    skipped = False
    for vid in vids:
        record = known.get(int(vid))
        if record is None:
            current.append(int(vid))
        elif (record[0], record[1]) != (model, dim):
            skipped = True
        else:
            current.append(int(vid))
    return current, skipped


def _vec_search_scoped(
    vec, query_vec, *, k: int, version_id: int, tenant_id: str | None = None
):
    try:
        return vec.search(query_vec, k=k, version_id=version_id, tenant_id=tenant_id)
    except TypeError:
        return vec.search(query_vec, k=k, version_id=version_id)
