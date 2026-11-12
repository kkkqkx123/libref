"""Persistence for entities, call instances, and manifest dependencies."""

from __future__ import annotations

import logging
from typing import Any

from libref.services.code.repo_ids import call_uid, entity_uid

logger = logging.getLogger(__name__)


def _persist_entities(
    conn, library_id: int, version_id: int, all_decls
) -> tuple[int, int]:
    import json as _decl_json

    pending: dict[tuple[str, str], Any] = {}
    for decl in all_decls:
        pending[(decl.qualified_name, decl.kind)] = decl
    conn.executemany(
        "INSERT INTO api_entities(entity_uid, library_id, version_id,"
        " qualified_name, kind, parent, signature, params, bases,"
        " start_byte, end_byte, depth, file, line_start, line_end,"
        " param_types, return_type, has_doc) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(entity_uid) DO UPDATE SET parent=excluded.parent,"
        " signature=excluded.signature, params=excluded.params, bases=excluded.bases,"
        " start_byte=excluded.start_byte, end_byte=excluded.end_byte,"
        " depth=excluded.depth, file=excluded.file, line_start=excluded.line_start,"
        " line_end=excluded.line_end, param_types=excluded.param_types,"
        " return_type=excluded.return_type, has_doc=excluded.has_doc",
        [
            (
                entity_uid(version_id, qualified_name, kind),
                library_id,
                version_id,
                qualified_name,
                kind,
                decl.parent,
                decl.signature,
                _decl_json.dumps(list(decl.params), ensure_ascii=False),
                _decl_json.dumps(list(decl.bases), ensure_ascii=False),
                decl.start_byte,
                decl.end_byte,
                decl.depth,
                decl.file,
                decl.line_start,
                decl.line_end,
                _decl_json.dumps(list(decl.param_types), ensure_ascii=False),
                decl.return_type,
                1 if decl.has_doc else 0,
            )
            for (qualified_name, kind), decl in pending.items()
        ],
    )
    wanted = sorted(entity_uid(version_id, q, k) for q, k in pending)
    keep = set(wanted)
    existing = [
        r[0]
        for r in conn.execute(
            "SELECT entity_uid FROM api_entities WHERE version_id=?", (version_id,)
        ).fetchall()
    ]
    dead = [u for u in existing if u not in keep]
    swept = 0
    for start in range(0, len(dead), 500):
        batch = dead[start : start + 500]
        placeholders = ",".join("?" for _ in batch)
        cur = conn.execute(
            "DELETE FROM api_entities WHERE version_id=?"
            f" AND entity_uid IN ({placeholders})",
            (version_id, *batch),
        )
        swept += int(cur.rowcount or 0)
    return len(pending), swept


def _upsert_calls(
    conn, library_id: int, version_id: int, resolved, commit_sha: str = ""
) -> tuple[int, set[str]]:
    """Upsert one batch of call instances without sweeping; returns count and identity set."""
    import json as _uid_json

    from libref.mining import scope_key as _scope_key

    entity_ids = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT qualified_name || '|' || kind, id FROM api_entities"
            " WHERE version_id=?",
            (version_id,),
        )
    }
    kinds = ("function", "method", "class", "interface", "enum", "constructor")
    batch: list[tuple] = []
    review_uids: list[str] = []
    wanted: set[str] = set()
    for call in resolved:
        args_json = _uid_json.dumps(list(call.args), ensure_ascii=False)
        uid = call_uid(
            call.repo,
            call.caller_file,
            call.caller,
            call.start_byte,
            call.end_byte,
            call.callee,
            list(call.args),
        )
        wanted.add(uid)
        ent_id = None
        for kind in kinds:
            ent_id = entity_ids.get(f"{call.callee}|{kind}")
            if ent_id is not None:
                break
        caller_ent_id = None
        if call.caller:
            for kind in kinds:
                caller_ent_id = entity_ids.get(f"{call.caller}|{kind}")
                if caller_ent_id is not None:
                    break
        batch.append(
            (
                uid,
                ent_id,
                call.callee,
                args_json,
                call.repo,
                call.caller_file,
                call.line_start,
                call.line_end,
                call.confidence,
                library_id,
                version_id,
                call.caller,
                call.receiver,
                call.func,
                call.argument_count,
                call.call_order,
                call.start_byte,
                call.end_byte,
                commit_sha or call.commit_sha,
                _scope_key(str(call.caller_file), call.caller),
                call.resolve_kind,
                caller_ent_id,
            )
        )
        needs_review = call.confidence == "AMBIGUOUS" or (
            call.confidence == "INFERRED" and ent_id is None
        )
        if needs_review:
            review_uids.append(uid)
    if batch:
        conn.executemany(
            "INSERT INTO call_instances(call_uid, callee_entity_id, callee_name, args,"
            " repo, caller_file, line_start, line_end, confidence,"
            " library_id, version_id, caller, receiver, func, argument_count,"
            " call_order, start_byte, end_byte, commit_sha, scope,"
            " resolve_kind, caller_entity_id)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(call_uid) DO UPDATE SET callee_entity_id=excluded.callee_entity_id,"
            " callee_name=excluded.callee_name, args=excluded.args,"
            " confidence=excluded.confidence, library_id=excluded.library_id,"
            " version_id=excluded.version_id, caller=excluded.caller,"
            " receiver=excluded.receiver, func=excluded.func,"
            " argument_count=excluded.argument_count, call_order=excluded.call_order,"
            " start_byte=excluded.start_byte, end_byte=excluded.end_byte,"
            " commit_sha=excluded.commit_sha, scope=excluded.scope,"
            " resolve_kind=excluded.resolve_kind, caller_entity_id=excluded.caller_entity_id",
            batch,
        )
    if review_uids:
        pending_ids: list[int] = []
        for start in range(0, len(review_uids), 500):
            chunk = review_uids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            pending_ids.extend(
                int(r[0])
                for r in conn.execute(
                    f"SELECT id FROM call_instances WHERE call_uid IN ({placeholders})",
                    chunk,
                ).fetchall()
            )
        if pending_ids:
            conn.executemany(
                "INSERT INTO review_status(entity_type, entity_id, status)"
                " VALUES('call_instance', ?, 'pending')"
                " ON CONFLICT(entity_type, entity_id) DO NOTHING",
                [(i,) for i in pending_ids],
            )
    return len(resolved), set(wanted)


def _sweep_calls(conn, version_id: int, wanted: set[str]) -> int:
    """Delete call instances outside the surviving identity set; version-level sweep runs once."""
    keep = set(wanted)
    existing = [
        (int(r[0]), str(r[1]))
        for r in conn.execute(
            "SELECT id, call_uid FROM call_instances WHERE version_id=?", (version_id,)
        ).fetchall()
    ]
    dead_rows = [(i, u) for i, u in existing if u not in keep]
    swept = 0
    for start in range(0, len(dead_rows), 500):
        swept_chunk = dead_rows[start : start + 500]
        placeholders = ",".join("?" for _ in swept_chunk)
        conn.execute(
            "DELETE FROM review_status WHERE entity_type='call_instance'"
            f" AND entity_id IN ({placeholders})",
            [i for i, _ in swept_chunk],
        )
        cur = conn.execute(
            "DELETE FROM call_instances WHERE version_id=?"
            f" AND call_uid IN ({placeholders})",
            (version_id, *[u for _, u in swept_chunk]),
        )
        swept += int(cur.rowcount or 0)
    return swept


def _persist_calls(
    conn, library_id: int, version_id: int, resolved, commit_sha: str = ""
) -> tuple[int, int]:
    """Upsert plus version-level sweep; batched ingest calls _upsert_calls per batch instead."""
    count, wanted = _upsert_calls(conn, library_id, version_id, resolved, commit_sha)
    return count, _sweep_calls(conn, version_id, wanted)


def _import_kind(importer_file: str, alias: str, target: str) -> str:
    """Import edge shape by file language plus alias and target relation."""
    from pathlib import Path as _Path

    from libref.astminer import config as _ast_config

    cfg = _ast_config.config_for_suffix(_Path(importer_file).suffix)
    lang = cfg.name if cfg is not None else ""
    if lang == "python":
        if alias == target:
            return "python:import"
        if target.endswith("." + alias):
            return "python:from"
        return "python:import-as"
    if lang == "rust":
        return "rust:use"
    if lang == "csharp":
        return "csharp:using"
    if lang == "go":
        return "go:import"
    if lang in ("java", "kotlin", "groovy"):
        return f"{lang}:import"
    if lang in ("javascript", "typescript"):
        return f"{lang}:import"
    return "import"


def _persist_import_edges(conn, library_id: int, version_id: int, file_aliases) -> int:
    rows: list[tuple] = []
    for importer_file, items in (file_aliases or {}).items():
        seen: set[str] = set()
        for alias, target in items:
            if not alias or not target or alias in seen:
                continue
            seen.add(alias)
            rows.append(
                (
                    library_id,
                    version_id,
                    importer_file,
                    alias,
                    target,
                    _import_kind(importer_file, alias, target),
                )
            )
    conn.execute("DELETE FROM import_edges WHERE version_id=?", (version_id,))
    if rows:
        conn.executemany(
            "INSERT INTO import_edges(library_id, version_id, importer_file, alias, target, kind)"
            " VALUES(?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(version_id, importer_file, alias)"
            " DO UPDATE SET target=excluded.target, kind=excluded.kind",
            rows,
        )
    return len(rows)


def _check_manifest_identity(ecosystem: str, library: str, root) -> None:
    """Warn when the checked-out manifests declare a different identity; never blocks."""
    from libref.astminer import manifests as _manifests
    from libref.crawler.registry import SUPPORTED_ECOSYSTEMS, canonical_ecosystem

    eco = canonical_ecosystem(ecosystem) if ecosystem else None
    if eco not in SUPPORTED_ECOSYSTEMS:
        return
    declared = _manifests.manifest_identity(root)
    if declared is None or declared == (eco, library):
        return
    logger.warning(
        "manifest identity mismatch: requested %s:%s declared %s:%s; continuing",
        eco,
        library,
        declared[0],
        declared[1],
    )


def _persist_repo_deps(conn, library_id: int, version_id: int, root) -> int:
    """Manifest-declared dependency edges: version-scoped full refresh, unparsable manifests yield nothing."""
    from libref.astminer import gradle as _gradle
    from libref.astminer import manifests as _manifests

    deps = _manifests.manifest_dependencies(root) + _gradle.gradle_dependencies(root)
    conn.execute("DELETE FROM repo_dependencies WHERE version_id=?", (version_id,))
    if deps:
        conn.executemany(
            "INSERT INTO repo_dependencies(library_id, version_id, ecosystem, name, req, source_file)"
            " VALUES(?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(version_id, ecosystem, name, source_file) DO UPDATE SET req=excluded.req",
            [
                (
                    library_id,
                    version_id,
                    d.ecosystem,
                    d.name,
                    d.req,
                    d.source_file,
                )
                for d in deps
            ],
        )
    conn.commit()
    return len(deps)
