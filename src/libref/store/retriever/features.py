"""Document feature filters, bonuses and alignment keys."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from libref.store.retriever.candidates import _profile_id_by_version


def filter_by_doc_facets(
    conn: sqlite3.Connection,
    doc_ids: list[int],
    *,
    block_type: str | None = None,
    exclude_deprecated: bool = False,
    aligned_only: bool = False,
) -> list[int]:
    """Apply document-derived filters after fusion, before rerank and assembly."""
    if not doc_ids or not (block_type or exclude_deprecated or aligned_only):
        return doc_ids
    try:
        conn.execute("SELECT 1 FROM doc_features LIMIT 0").fetchone()
        conn.execute("SELECT 1 FROM doc_api_links LIMIT 0").fetchone()
    except sqlite3.Error:
        return doc_ids
    marks = ",".join("?" for _ in doc_ids)
    keep: set[int] = set(doc_ids)
    if block_type:
        rows = conn.execute(
            "SELECT DISTINCT document_id FROM doc_features WHERE document_id IN ("
            + marks
            + ") AND facet='block_type' AND json_extract(value, '$.kind')=?",
            [*doc_ids, block_type],
        ).fetchall()
        keep &= {int(row[0]) for row in rows}
    if exclude_deprecated:
        rows = conn.execute(
            "SELECT DISTINCT document_id FROM doc_features WHERE document_id IN ("
            + marks
            + ") AND facet='deprecated' AND json_extract(value, '$.details.value')=1",
            doc_ids,
        ).fetchall()
        keep -= {int(row[0]) for row in rows}
    if aligned_only:
        rows = conn.execute(
            "SELECT DISTINCT document_id FROM doc_api_links WHERE document_id IN ("
            + marks
            + ") AND match_kind IN ('symbol', 'signature')",
            doc_ids,
        ).fetchall()
        keep &= {int(row[0]) for row in rows}
    return [doc for doc in doc_ids if doc in keep]


ALIGNMENT_BONUS = {"symbol": 0.1, "signature": 0.05}


def _doc_feature_bonuses(
    conn: sqlite3.Connection, vids: list[int]
) -> dict[int, dict[int, float]]:
    """Per-version document bonuses from example facets, deprecated marks, and links."""
    if not vids:
        return {}
    try:
        conn.execute("SELECT 1 FROM doc_features LIMIT 0").fetchone()
        conn.execute("SELECT 1 FROM doc_api_links LIMIT 0").fetchone()
    except sqlite3.Error:
        return {}
    marks = ",".join("?" for _ in vids)
    out: dict[int, dict[int, float]] = {}
    for version_id in vids:
        out[int(version_id)] = {}
    rows = conn.execute(
        "SELECT version_id, document_id, facet, value FROM doc_features"
        f" WHERE version_id IN ({marks})"
        " AND facet IN ('block_type', 'deprecated', 'code_ratio')",
        vids,
    ).fetchall()
    for version_id, doc_id, facet, raw in rows:
        try:
            value = json.loads(raw) if raw else {}
        except TypeError, ValueError:
            value = {}
        slot = out.setdefault(int(version_id), {}).setdefault(int(doc_id), 0.0)
        if facet == "block_type" and value.get("kind") == "example":
            out[int(version_id)][int(doc_id)] = slot + 0.5
        elif (
            facet == "code_ratio"
            and float(value.get("details", {}).get("value") or 0) > 0
        ):
            out[int(version_id)][int(doc_id)] = slot + 0.25
        elif facet == "deprecated" and value.get("details", {}).get("value"):
            out[int(version_id)][int(doc_id)] = slot - 0.5
    link_rows = conn.execute(
        "SELECT version_id, document_id, match_kind, count(*) FROM doc_api_links"
        f" WHERE version_id IN ({marks}) GROUP BY version_id, document_id, match_kind",
        vids,
    ).fetchall()
    for version_id, doc_id, kind, count in link_rows:
        slot = out.setdefault(int(version_id), {}).setdefault(int(doc_id), 0.0)
        out[int(version_id)][int(doc_id)] = slot + min(5, int(count)) * float(
            ALIGNMENT_BONUS.get(str(kind), 0.0)
        )
    return {
        vid: {doc: max(0.0, min(1.0, value)) for doc, value in rows.items()}
        for vid, rows in out.items()
    }


def alignment_key(meta: dict[str, Any] | None, url: str | None, doc_id: int) -> str:
    """Cross-path alignment key: document section first, chunk id next, entity as fallback.

    Section-first keeps twin chunks from both lanes together while unrelated
    sections sharing one entity no longer collapse each other.
    """
    data = meta or {}
    group = str(data.get("source_group_id") or "").strip()
    if group:
        return f"s:{url or ''}\x00{group}"
    chunk_id = str(data.get("chunk_id") or "").strip()
    if chunk_id:
        return f"c:{chunk_id}"
    entities = data.get("entities") or []
    if entities:
        first = str(entities[0]).strip()
        if first:
            return f"e:{first}"
    return f"c:{doc_id}"


def _pattern_supports(
    conn: sqlite3.Connection,
    version_ids: list[int],
    wanted: set[str] | None = None,
) -> dict[int, list[tuple[list[str], int]]]:
    if not version_ids:
        return {}
    profiles = _profile_id_by_version(conn, version_ids)
    if not profiles:
        return {}
    marks = ",".join("?" for _ in profiles.values())
    rows = conn.execute(
        "SELECT version_id, pattern, support FROM seq_patterns "
        f"WHERE profile_id IN ({marks})",
        tuple(profiles.values()),
    ).fetchall()
    out: dict[int, list[tuple[list[str], int]]] = {}
    for vid, raw, support in rows:
        try:
            names = json.loads(raw)
        except ValueError:
            continue
        if wanted and not set(names).intersection(wanted):
            continue
        out.setdefault(int(vid), []).append((names, int(support)))
    for vid in list(out):
        out[vid].sort(key=lambda item: (-item[1], item[0]))
        del out[vid][500:]
    return out


def _facet_weights(
    conn: sqlite3.Connection, version_ids: list[int]
) -> dict[int, list[tuple[str, float]]]:
    """Per-version callee weights from stored arg-shape facets."""
    from libref.mining.facets import FACET_ARG_SHAPE, facet_callee

    if not version_ids:
        return {}
    profiles = _profile_id_by_version(conn, version_ids)
    if not profiles:
        return {}
    try:
        conn.execute("SELECT 1 FROM feature_facets LIMIT 1").fetchone()
    except Exception:  # noqa: BLE001 -- facets are an optional derived table
        return {}
    marks = ",".join("?" for _ in profiles.values())
    rows = conn.execute(
        "SELECT version_id, value, calls FROM feature_facets "
        f"WHERE profile_id IN ({marks}) AND facet=?",
        (*profiles.values(), FACET_ARG_SHAPE),
    ).fetchall()
    ceiling: dict[int, int] = {}
    for vid, _value, calls in rows:
        ceiling[int(vid)] = max(ceiling.get(int(vid), 0), int(calls))
    out: dict[int, list[tuple[str, float]]] = {}
    for vid, value, calls in rows:
        top = ceiling.get(int(vid), 0)
        weight = (int(calls) / top) if top > 0 else 0.0
        out.setdefault(int(vid), []).append((facet_callee(str(value)), weight))
    return out


def _cluster_sets(
    conn: sqlite3.Connection, version_ids: list[int]
) -> dict[int, list[set[str]]]:
    if not version_ids:
        return {}
    profiles = _profile_id_by_version(conn, version_ids)
    if not profiles:
        return {}
    marks = ",".join("?" for _ in profiles.values())
    rows = conn.execute(
        "SELECT version_id, cluster_id, member FROM api_clusters "
        f"WHERE profile_id IN ({marks})",
        tuple(profiles.values()),
    ).fetchall()
    grouped: dict[tuple[int, int], set[str]] = {}
    for vid, cid, member in rows:
        grouped.setdefault((int(vid), int(cid)), set()).add(str(member))
    out: dict[int, list[set[str]]] = {}
    for (vid, _), members in grouped.items():
        out.setdefault(vid, []).append(members)
    return out
