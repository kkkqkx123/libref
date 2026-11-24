"""Document feature derivation: rebuild facets, alignment edges, coverage, and verdicts.

The source of truth stays in documents, entities, and call instances. Every table
written here is rebuildable, and a failed rebuild rolls back as one transaction.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections import defaultdict
from typing import Any

from libref.astminer.models import TARGET_STATUS_RESOLVED
from libref.chunking.doc_facets import (
    DocBlock,
    aggregate_doc_facets,
    code_hash,
    fence_codes,
)
from libref.mining.doc_alignment import (
    align_documents,
    build_api_index,
    compare_consistency,
    cover_patterns,
    pattern_hash,
)


def _json_list(raw) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(str(raw))
    except TypeError, ValueError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


SCOPES = (
    "structure",
    "links",
    "sources",
    "coverage",
    "consistency",
    "derived",
    "all",
)
_ORDINAL_RE = re.compile(r"#(\d+)")
_BATCH = 500


def _setting(settings, name: str, default):
    if settings is not None and hasattr(settings, name):
        return getattr(settings, name)
    return default


def _table_ready(conn, name: str) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {name} LIMIT 0").fetchone()
    except Exception:  # noqa: BLE001 — absent derived table is an empty result
        return False
    return True


def _load_blocks(conn, version_id: int, tenant_id: str) -> list[DocBlock]:
    sql = "SELECT id, url, content, metadata FROM documents WHERE version_id=?"
    params: list[Any] = [version_id]
    if tenant_id not in (None, "*", "global"):
        try:
            from libref.services.infra.tenant import is_global, table_has_tenant

            if table_has_tenant(conn, "documents") and not is_global(str(tenant_id)):
                sql += " AND tenant_id=?"
                params.append(str(tenant_id))
        except Exception:  # noqa: BLE001, S110 — tenant helpers are optional on bare schema
            pass
    sql += " ORDER BY page_id, sort_order, id"
    out: list[DocBlock] = []
    for row in conn.execute(sql, params).fetchall():
        try:
            meta = json.loads(row[3]) if row[3] else {}
        except TypeError, ValueError:
            meta = {}
        out.append(
            DocBlock(
                document_id=int(row[0]),
                content=str(row[2] or ""),
                url=str(row[1] or ""),
                path=str(meta.get("path") or ""),
                title=str(meta.get("title") or ""),
                chunk_path=str(meta.get("chunk_path") or "embedding"),
            )
        )
    return out


def _load_snippet_calls(conn, version_id: int, tenant_id: str) -> list[dict[str, Any]]:
    from libref.services.ops.review import ReviewService

    reviews = ReviewService(conn).review_map_for_version(version_id)
    sql = (
        "SELECT ci.id, ci.callee_name, ci.callee_entity_id, ci.sample_id, ci.caller_file,"
        " ci.confidence, ci.resolve_kind, ci.line_start, ci.call_order,"
        " ci.source_role, ci.source_id, ci.target_status"
        " FROM call_instances ci WHERE ci.version_id=?"
        " AND ci.source_role='doc_snippet'"
    )
    params: list[Any] = [version_id]
    if tenant_id not in (None, "*", "global"):
        try:
            from libref.services.infra.tenant import is_global, table_has_tenant

            if table_has_tenant(conn, "call_instances") and not is_global(
                str(tenant_id)
            ):
                sql += " AND ci.tenant_id=?"
                params.append(str(tenant_id))
        except Exception:  # noqa: BLE001, S110
            pass
    sql += " ORDER BY ci.caller_file, ci.sample_id, ci.line_start, ci.call_order"
    out: list[dict[str, Any]] = []
    for row in conn.execute(sql, params).fetchall():
        if reviews.get(int(row[0])) == "conflict":
            continue
        if str(row[11]) != TARGET_STATUS_RESOLVED:
            continue
        out.append(
            {
                "id": int(row[0]),
                "callee": str(row[1] or ""),
                "api_entity_id": int(row[2] or 0),
                "sample_id": str(row[3] or ""),
                "url": str(row[4] or ""),
                "confidence": str(row[5] or ""),
                "resolve_kind": str(row[6] or ""),
                "line": int(row[7] or 0),
                "order": int(row[8] or 0),
                "source_role": str(row[9] or "doc_snippet"),
                "source_id": row[10],
            }
        )
    return out


def _ordinal(sample_id: str) -> int:
    match = _ORDINAL_RE.search(str(sample_id or ""))
    return int(match.group(1)) if match else 0


def _map_calls_to_documents(
    blocks: list[DocBlock], calls: list[dict[str, Any]]
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, list[str]]]:
    """Attach snippet calls to chunks by page-scoped fence ordinal, then by name."""
    by_url: dict[str, list[DocBlock]] = defaultdict(list)
    for block in blocks:
        if block.url:
            by_url[block.url].append(block)
    hash_by_doc: dict[int, dict[int, str]] = {}
    for url, group in by_url.items():
        del url
        ordinal = 0
        for block in sorted(group, key=lambda b: b.document_id):
            mapping: dict[int, str] = {}
            for code in fence_codes(block.content):
                ordinal += 1
                mapping[ordinal] = code_hash(code)
            hash_by_doc[block.document_id] = mapping
    mapped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    symbols: dict[int, list[str]] = defaultdict(list)
    for call in calls:
        url = str(call.get("url") or "")
        group = by_url.get(url, [])
        ordinal = _ordinal(call["sample_id"])
        target = next(
            (b for b in group if ordinal in hash_by_doc.get(b.document_id, {})),
            None,
        )
        if target is None:
            low = str(call.get("callee") or "")
            target = next((b for b in group if low and low in b.content), None)
        if target is None:
            continue
        digest = hash_by_doc.get(target.document_id, {}).get(ordinal, "")
        enriched_call = {**call, "code_hash": digest}
        mapped[target.document_id].append(enriched_call)
        if enriched_call["callee"]:
            symbols[target.document_id].append(enriched_call["callee"])
    return dict(mapped), {
        key: list(dict.fromkeys(value)) for key, value in symbols.items()
    }


def _enriched_blocks(
    blocks: list[DocBlock],
    mapped: dict[int, list[dict[str, Any]]],
    deprecated_ids: set[int],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        calls = sorted(
            mapped.get(block.document_id, []),
            key=lambda c: (
                _ordinal(c["sample_id"]),
                c["line"],
                c["order"],
                c["callee"],
            ),
        )
        out.append(
            {
                "document_id": block.document_id,
                "url": block.url,
                "content": block.content,
                "code_hashes": list(
                    {code_hash(code) for code in fence_codes(block.content)}
                ),
                "call_sequence": [c["callee"] for c in calls if c["callee"]],
                "deprecated": block.document_id in deprecated_ids,
                "calls": calls,
            }
        )
    return out


def _write_features(
    conn, library_id: int, version_id: int, rows, tenant_id: str
) -> int:
    from libref.services.infra.tenant import normalize_tenant

    stmt = (
        "INSERT INTO doc_features(library_id, version_id, document_id, facet, value, tenant_id)"
        " VALUES(?,?,?,?,?,?) ON CONFLICT(version_id, document_id, facet)"
        " DO UPDATE SET value=excluded.value"
    )
    data = [
        (
            library_id,
            version_id,
            row.document_id,
            row.facet,
            json.dumps(row.value, ensure_ascii=False),
            normalize_tenant(tenant_id),
        )
        for row in rows
    ]
    if data:
        conn.executemany(stmt, data)
    return len(data)


def _write_links(
    conn, library_id: int, version_id: int, rows: list[dict], tenant_id: str
) -> int:
    from libref.services.infra.tenant import normalize_tenant

    stmt = (
        "INSERT INTO doc_api_links(library_id, version_id, document_id, api_entity_id,"
        " match_kind, confidence, evidence, tenant_id) VALUES(?,?,?,?,?,?,?,?)"
        " ON CONFLICT(version_id, document_id, api_entity_id) DO UPDATE SET"
        " match_kind=excluded.match_kind, confidence=excluded.confidence,"
        " evidence=excluded.evidence"
    )
    data = [
        (
            library_id,
            version_id,
            int(row["document_id"]),
            int(row["api_entity_id"]),
            str(row["match_kind"]),
            float(row["confidence"]),
            json.dumps(row["evidence"], ensure_ascii=False),
            normalize_tenant(tenant_id),
        )
        for row in rows
    ]
    if data:
        conn.executemany(stmt, data)
    live = {(int(r["document_id"]), int(r["api_entity_id"])) for r in rows}
    if live:
        existing = {
            (int(r[0]), int(r[1]))
            for r in conn.execute(
                "SELECT document_id, api_entity_id FROM doc_api_links WHERE version_id=?",
                (version_id,),
            ).fetchall()
        }
        stale = existing - live
        for start in range(0, len(stale), _BATCH):
            batch = list(stale)[start : start + _BATCH]
            marks = ",".join("(?,?)" for _ in batch)
            conn.execute(
                f"DELETE FROM doc_api_links WHERE version_id=? AND (document_id, api_entity_id) IN ({marks})",
                [version_id, *[item for pair in batch for item in pair]],
            )
    return len(data)


def _backfill_entities(conn, version_id: int) -> int:
    rows = conn.execute(
        "SELECT document_id, ae.qualified_name FROM doc_api_links l"
        " JOIN api_entities ae ON ae.id=l.api_entity_id"
        " WHERE l.version_id=? ORDER BY document_id, ae.qualified_name",
        (version_id,),
    ).fetchall()
    wanted: dict[int, list[str]] = defaultdict(list)
    for doc_id, name in rows:
        wanted[int(doc_id)].append(str(name))
    doc_rows = conn.execute(
        "SELECT id, metadata FROM documents WHERE version_id=?", (version_id,)
    ).fetchall()
    changed = 0
    for doc_id, raw in doc_rows:
        try:
            meta = json.loads(raw) if raw else {}
        except TypeError, ValueError:
            meta = {}
        entities = list(dict.fromkeys(wanted.get(int(doc_id), [])))
        if list(meta.get("entities") or []) == entities:
            continue
        meta["entities"] = entities
        conn.execute(
            "UPDATE documents SET metadata=? WHERE id=?",
            (json.dumps(meta, ensure_ascii=False), int(doc_id)),
        )
        changed += 1
    return changed


def _write_coverage(
    conn,
    library_id: int,
    version_id: int,
    profile_id: int,
    rows: list[dict],
    tenant_id: str,
) -> int:
    from libref.services.infra.tenant import normalize_tenant

    stmt = (
        "INSERT INTO doc_pattern_coverage(library_id, version_id, profile_id, document_id,"
        " pattern_hash, pattern, covered_kind, support, tenant_id)"
        " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(profile_id, document_id, pattern_hash, covered_kind)"
        " DO UPDATE SET pattern=excluded.pattern, support=excluded.support"
    )
    data = [
        (
            library_id,
            version_id,
            profile_id,
            int(row["document_id"]),
            str(row["pattern_hash"]),
            json.dumps(row["pattern"], ensure_ascii=False),
            str(row["covered_kind"]),
            int(row["support"]),
            normalize_tenant(tenant_id),
        )
        for row in rows
    ]
    if data:
        conn.executemany(stmt, data)
    live = {
        (int(r["document_id"]), str(r["pattern_hash"]), str(r["covered_kind"]))
        for r in rows
    }
    existing = {
        (int(r[0]), str(r[1]), str(r[2]))
        for r in conn.execute(
            "SELECT document_id, pattern_hash, covered_kind FROM doc_pattern_coverage"
            " WHERE version_id=? AND profile_id=?",
            (version_id, profile_id),
        ).fetchall()
    }
    stale = existing - live
    for start in range(0, len(stale), _BATCH):
        batch = list(stale)[start : start + _BATCH]
        for doc_id, digest, kind in batch:
            conn.execute(
                "DELETE FROM doc_pattern_coverage WHERE version_id=? AND profile_id=?"
                " AND document_id=? AND pattern_hash=? AND covered_kind=?",
                (version_id, profile_id, doc_id, digest, kind),
            )
    return len(data)


def _write_consistency(
    conn,
    library_id: int,
    version_id: int,
    profile_id: int,
    rows: list[dict],
    tenant_id: str,
    settings,
    chat,
) -> int:
    from libref.services.infra.tenant import normalize_tenant

    batch_size = int(getattr(settings, "doc_consistency_batch", 8) or 8)
    prompt_version = str(getattr(settings, "induction_prompt_version", "v1") or "v1")
    conn.execute(
        "DELETE FROM doc_consistency WHERE version_id=? AND profile_id=?",
        (version_id, profile_id),
    )
    notes: dict[int, str] = {}
    if bool(getattr(settings, "doc_consistency_llm_enabled", False)) and callable(chat):
        drift = [row for row in rows if row["verdict"] == "drift"][:batch_size]
        for row in drift:
            messages = [
                {
                    "role": "system",
                    "content": "Summarize documentation drift in one short sentence. Do not claim correctness.",
                },
                {
                    "role": "user",
                    "content": json.dumps(row["evidence"], ensure_ascii=False),
                },
            ]
            try:
                result = chat(messages)
                if inspect.isawaitable(result):
                    result = asyncio.run(result)
                notes[int(row["document_id"])] = str(result).strip()[:500]
            except Exception:  # noqa: BLE001, S112 — model output is advisory only
                continue
    stmt = (
        "INSERT INTO doc_consistency(library_id, version_id, profile_id, document_id, facet,"
        " verdict, note, evidence_hash, model, prompt_version, tenant_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(profile_id, document_id, facet, model, prompt_version)"
        " DO UPDATE SET verdict=excluded.verdict, note=excluded.note,"
        " evidence_hash=excluded.evidence_hash, updated_at=datetime('now')"
    )
    data = [
        (
            library_id,
            version_id,
            profile_id,
            int(row["document_id"]),
            str(row["facet"]),
            str(row["verdict"]),
            notes.get(int(row["document_id"]), ""),
            str(row["evidence_hash"]),
            "model" if int(row["document_id"]) in notes else "rule",
            prompt_version if int(row["document_id"]) in notes else "",
            normalize_tenant(tenant_id),
        )
        for row in rows
    ]
    if data:
        conn.executemany(stmt, data)
    return len(data)


def refresh_doc_features(
    conn,
    library: str,
    version: str,
    *,
    ecosystem: str | None = None,
    profile: str | None = None,
    scope: str = "all",
    tenant_id: str = "default",
    settings=None,
    chat=None,
) -> dict[str, int]:
    """Rebuild the requested document-derived facets in one transaction."""
    if scope not in SCOPES:
        raise ValueError(f"unsupported doc feature scope: {scope}")
    from libref.chunking.doc_facets import DOC_FACETS
    from libref.services.registry.libraries import LibraryService

    counts = {
        "features": 0,
        "links": 0,
        "entities_updated": 0,
        "coverage": 0,
        "consistency": 0,
        "dropped": 0,
    }
    if not bool(_setting(settings, "doc_feature_enabled", True)):
        return counts
    if scope == "all":
        wanted = {"structure", "links", "coverage", "consistency"}
    elif scope == "sources":
        wanted = {"structure", "links"}
    elif scope == "derived":
        wanted = {"coverage", "consistency"}
    else:
        wanted = {scope}
    needed_tables = ["doc_features", "doc_api_links"]
    if "coverage" in wanted or "consistency" in wanted:
        needed_tables.extend(["doc_pattern_coverage", "doc_consistency"])
    if any(not _table_ready(conn, name) for name in needed_tables):
        return counts

    catalog = LibraryService(conn)
    if ecosystem is None:
        library_id, version_id = catalog.ensure_library_version(library, version)
    else:
        library_id, version_id = catalog.ensure_library_version(
            library, version, ecosystem
        )
    entity_version_id = catalog.entity_source_version_id(
        version_id, tenant_id=tenant_id
    )
    try:
        for table in ("doc_consistency", "doc_pattern_coverage"):
            if table in needed_tables:
                conn.execute(f"DELETE FROM {table} WHERE version_id=?", (version_id,))
        if "links" in wanted:
            conn.execute("DELETE FROM doc_api_links WHERE version_id=?", (version_id,))
        if "structure" in wanted:
            conn.execute("DELETE FROM doc_features WHERE version_id=?", (version_id,))

        blocks = _load_blocks(conn, version_id, tenant_id)
        if not blocks:
            conn.commit()
            return counts
        calls = _load_snippet_calls(conn, version_id, tenant_id)
        mapped, snippet_symbols = _map_calls_to_documents(blocks, calls)

        entity_rows = conn.execute(
            "SELECT id, qualified_name, params, return_type, signature FROM api_entities"
            " WHERE version_id=?",
            (entity_version_id,),
        ).fetchall()
        entities = [
            {"id": int(r[0]), "qualified_name": str(r[1] or "")} for r in entity_rows
        ]
        entity_signatures = {
            str(row[1]): {
                "qualified_name": str(row[1] or ""),
                "params": [str(item) for item in _json_list(row[2])],
                "return_type": str(row[3] or ""),
                "signature": str(row[4] or ""),
            }
            for row in entity_rows
        }
        index = build_api_index(entities)

        if "structure" in wanted:
            enabled = list(
                _setting(
                    settings,
                    "doc_feature_enabled_facets",
                    list(DOC_FACETS),
                )
                or DOC_FACETS
            )
            result = aggregate_doc_facets(
                blocks,
                enabled_facets=enabled,
                terms_per_facet=int(
                    _setting(settings, "doc_feature_terms_per_facet", 20)
                ),
                candidate_budget=int(
                    _setting(settings, "doc_feature_candidate_budget", 200)
                ),
                snippet_symbols=snippet_symbols,
                entity_signatures=entity_signatures,
            )
            counts["features"] = _write_features(
                conn, library_id, version_id, result.rows, tenant_id
            )
            counts["dropped"] = result.dropped

        deprecated_ids = {
            int(row[0])
            for row in conn.execute(
                "SELECT document_id FROM doc_features WHERE version_id=?"
                " AND facet='deprecated' AND json_extract(value, '$.details.value')=1",
                (version_id,),
            ).fetchall()
        }
        enriched = _enriched_blocks(blocks, mapped, deprecated_ids)
        if "links" in wanted:
            links = align_documents(
                enriched,
                index,
                [call for group in mapped.values() for call in group],
                min_confidence=float(
                    _setting(settings, "doc_feature_min_confidence", 0.4)
                ),
            )
            counts["links"] = _write_links(
                conn, library_id, version_id, links, tenant_id
            )
            counts["entities_updated"] = _backfill_entities(conn, version_id)

        if "coverage" in wanted or "consistency" in wanted:
            from libref.services.code.usage_sources import resolve_profile

            resolved = resolve_profile(
                conn,
                version_id=version_id,
                name=profile,
                tenant_id=tenant_id,
            )
            profile_id = int(resolved["id"])
            patterns = [
                {
                    "pattern": json.loads(row[0]) if row[0] else [],
                    "support": int(row[1] or 0),
                }
                for row in conn.execute(
                    "SELECT pattern, support FROM seq_patterns WHERE version_id=?"
                    " AND profile_id=? ORDER BY support DESC, pattern",
                    (version_id, profile_id),
                ).fetchall()
            ]
            for pattern in patterns:
                pattern["pattern_hash"] = pattern_hash(
                    [str(item) for item in pattern["pattern"]]
                )
            min_support = int(_setting(settings, "doc_consistency_min_support", 2))
            coverage = cover_patterns(enriched, patterns, min_support=min_support)
            if "coverage" in wanted:
                counts["coverage"] = _write_coverage(
                    conn, library_id, version_id, profile_id, coverage, tenant_id
                )
            if "consistency" in wanted:
                links = [
                    {
                        "document_id": r[0],
                        "api_entity_id": r[1],
                    }
                    for r in conn.execute(
                        "SELECT document_id, api_entity_id FROM doc_api_links WHERE version_id=?",
                        (version_id,),
                    ).fetchall()
                ]
                verdicts = compare_consistency(
                    enriched, links, coverage, min_support=min_support
                )
                counts["consistency"] = _write_consistency(
                    conn,
                    library_id,
                    version_id,
                    profile_id,
                    verdicts,
                    tenant_id,
                    settings,
                    chat,
                )
        conn.commit()
        return counts
    except BaseException:
        conn.rollback()
        raise


def build_doc_feature_runner(settings, conn):
    """Build the job consumer for document feature derivation."""
    from libref.services.infra.jobs import DOC_FEATURE_KIND

    async def runner(job):
        if job.kind != DOC_FEATURE_KIND:
            raise ValueError(f"unsupported job kind: {job.kind}")
        p = job.params
        return refresh_doc_features(
            conn,
            p["library"],
            p["version"],
            ecosystem=p.get("ecosystem"),
            profile=p.get("profile"),
            scope=p.get("scope", "all"),
            tenant_id=p.get("tenant_id", "default"),
            settings=settings,
        )

    return runner
