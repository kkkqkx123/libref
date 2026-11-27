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


def doc_feature_annotations(
    conn: sqlite3.Connection,
    vids: list[int],
    doc_ids: list[int],
    *,
    max_items: int = 5,
) -> dict[int, list[str]]:
    """Collect derived facts for post-rank result enhancement; never a score input."""
    if not vids or not doc_ids or max_items <= 0:
        return {}
    version_marks = ",".join("?" for _ in vids)
    doc_marks = ",".join("?" for _ in doc_ids)
    params = [*vids, *doc_ids]
    out: dict[int, list[str]] = {int(doc_id): [] for doc_id in doc_ids}
    rows = conn.execute(
        "SELECT document_id, facet, value FROM doc_features"
        f" WHERE version_id IN ({version_marks})"
        f" AND document_id IN ({doc_marks})"
        " AND facet IN ('block_type', 'deprecated', 'code_ratio')"
        " ORDER BY document_id, facet",
        params,
    ).fetchall()
    for doc_id, facet, raw in rows:
        try:
            value = json.loads(raw) if raw else {}
        except TypeError, ValueError:
            continue
        doc = int(doc_id)
        if facet == "block_type" and value.get("kind") == "example":
            out.setdefault(doc, []).append("block: example")
        elif facet == "deprecated" and value.get("details", {}).get("value"):
            out.setdefault(doc, []).append("status: deprecated")
        elif facet == "code_ratio":
            ratio = float(value.get("details", {}).get("value") or 0)
            if ratio > 0:
                out.setdefault(doc, []).append(f"code ratio: {ratio:.2f}")
    link_rows = conn.execute(
        "SELECT l.document_id, ae.qualified_name FROM doc_api_links l"
        " JOIN api_entities ae ON ae.id=l.api_entity_id"
        f" WHERE l.version_id IN ({version_marks})"
        f" AND l.document_id IN ({doc_marks})"
        " AND l.match_kind IN ('symbol', 'signature')"
        " ORDER BY l.document_id, ae.qualified_name LIMIT 500",
        params,
    ).fetchall()
    by_doc: dict[int, list[str]] = {}
    for doc_id, name in link_rows:
        names = by_doc.setdefault(int(doc_id), [])
        if str(name) not in names:
            names.append(str(name))
    for doc_id, names in by_doc.items():
        out.setdefault(doc_id, []).append("aligned: " + ", ".join(names[:max_items]))
    clusters = _cluster_sets(conn, vids)
    member_rows = conn.execute(
        "SELECT version_id, document_id, api_entity_id FROM doc_api_links"
        f" WHERE version_id IN ({version_marks})"
        f" AND document_id IN ({doc_marks})"
        " AND match_kind IN ('symbol', 'signature')",
        params,
    ).fetchall()
    entity_names = dict(
        conn.execute(
            "SELECT id, qualified_name FROM api_entities"
            f" WHERE version_id IN ({version_marks})",
            vids,
        ).fetchall()
    )
    for version_id, doc_id, entity_id in member_rows:
        name = str(entity_names.get(int(entity_id)) or "")
        if not name:
            continue
        related = [
            member
            for members in clusters.get(int(version_id), [])
            if name in members
            for member in members
            if member != name
        ][:max_items]
        if related:
            out.setdefault(int(doc_id), []).append(
                "related: " + ", ".join(sorted(related))
            )
    return {doc: items[:max_items] for doc, items in out.items() if items}


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
