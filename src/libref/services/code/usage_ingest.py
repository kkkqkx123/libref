"""Ingestion of documentation and reference snippets as usage evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from libref.astminer.code_blocks import dedupe_snippets, suffix_for_language
from libref.astminer.config import config_for_suffix
from libref.astminer.models import ApiDeclRecord, CodeSnippet
from libref.services.code.repo_persist import _sweep_calls, _upsert_calls
from libref.services.code.usage_sources import register_source, source_target_status

_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def doc_source_key(version_id: int, url: str) -> str:
    """Identity of the usage source created from one documentation page."""
    return f"doc:{int(version_id)}:{url}"


def target_declarations(
    conn,
    version_id: int,
    *,
    entity_version_id: int | None = None,
    names: set[str] | list[str] | tuple[str, ...] | None = None,
    prefixes: tuple[str, ...] | list[str] | None = None,
) -> list[ApiDeclRecord]:
    """Load target API facts narrowed to names or prefixes when supplied."""
    sql = (
        "SELECT qualified_name, kind, file, line_start, line_end, params,"
        " param_types, return_type, bases, parent, signature, has_doc"
        " FROM api_entities WHERE version_id=?"
    )
    params: list[Any] = [int(entity_version_id or version_id)]
    wanted = sorted({str(name) for name in (names or []) if str(name).strip()})
    heads = tuple(str(value) for value in (prefixes or ()) if str(value).strip())
    if wanted:
        marks = ",".join("?" for _ in wanted)
        sql += f" AND qualified_name IN ({marks})"
        params.extend(wanted)
    elif heads:
        escaped = [
            head.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for head in heads
        ]
        predicate = "qualified_name LIKE ? ESCAPE '\\'"
        sql += " AND (" + " OR ".join([predicate] * len(escaped)) + ")"
        params.extend(escaped)
    rows = conn.execute(sql, tuple(params)).fetchall()
    out: list[ApiDeclRecord] = []
    for row in rows:
        out.append(
            ApiDeclRecord(
                qualified_name=str(row[0]),
                kind=str(row[1]),
                file=str(row[2] or ""),
                line_start=int(row[3] or 1),
                line_end=int(row[4] or 1),
                params=_json_list(row[5]),
                param_types=_json_list(row[6]),
                return_type=str(row[7] or ""),
                bases=_json_list(row[8]),
                parent=str(row[9]) if row[9] else None,
                signature=str(row[10] or ""),
                has_doc=bool(row[11]),
            )
        )
    return out


def _json_list(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(v) for v in raw]
    try:
        value = json.loads(str(raw))
    except ValueError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _synthetic_path(source_key: str, sample_id: str, suffix: str) -> str:
    safe = _SAFE_RE.sub("_", f"{source_key}_{sample_id}")[:160]
    return f"usage/{safe}{suffix}"


def _relocate_snippet_calls(resolved, snippet: CodeSnippet):
    """Move snippet call evidence from the synthetic path to its host source."""
    if not snippet.origin_location:
        return resolved
    for call in resolved:
        call.caller_file = snippet.origin_location
        if snippet.origin_start_line:
            call.line_start = snippet.origin_start_line + call.line_start - 1
            call.line_end = snippet.origin_start_line + call.line_end - 1
        if snippet.origin_start_byte:
            call.start_byte = snippet.origin_start_byte + call.start_byte
            call.end_byte = snippet.origin_start_byte + call.end_byte
        call.sample_id = (
            f"{snippet.sample_id}::{call.caller}" if call.caller else snippet.sample_id
        )
    return resolved


def ingest_snippets(
    conn,
    library: str,
    version: str,
    snippets: Iterable[CodeSnippet],
    *,
    source_key: str,
    source_role: str = "doc_snippet",
    origin_kind: str = "markdown_fence",
    ecosystem: str = "custom",
    tenant_id: str = "default",
    target_filter: dict | None = None,
    source_url: str = "",
    commit_sha: str = "",
    selection: dict | None = None,
    limits: dict | None = None,
    weight: float = 1.0,
    settings=None,
) -> dict[str, int]:
    """Parse snippets with target API facts and persist source-aware call instances."""
    from libref.astminer import engine as ast_engine
    from libref.astminer import resolution as ast_resolution
    from libref.services.registry.libraries import LibraryService

    catalog = LibraryService(conn)
    library_id, version_id = catalog.ensure_library_version(
        library, version, ecosystem, tenant_id=tenant_id
    )
    source_id = register_source(
        conn,
        version_id=version_id,
        source_key=source_key,
        source_role=source_role,
        origin_kind=origin_kind,
        source_url=source_url,
        commit_sha=commit_sha,
        target_filter=target_filter or {},
        selection=selection,
        limits=limits,
        weight=weight,
        tenant_id=tenant_id,
    )
    entity_version_id = catalog.entity_source_version_id(
        version_id, tenant_id=tenant_id
    )
    filt = target_filter or {}
    explicit_names = {str(v) for v in filt.get("names", []) if str(v).strip()}
    prefixes = tuple(str(v) for v in filt.get("prefixes", []) if str(v).strip())
    target_decls = target_declarations(
        conn,
        version_id,
        entity_version_id=entity_version_id,
        names=explicit_names or None,
        prefixes=None if explicit_names else prefixes,
    )
    target_names = {decl.qualified_name for decl in target_decls}
    source_status = source_target_status(
        source_role,
        filt.get("declared_library_version"),
        version,
        version_required=bool(filt.get("version_required")),
    )
    effective_names = explicit_names or target_names
    wanted: set[str] = set()
    calls = 0
    parsed = 0
    for snippet in dedupe_snippets(snippets):
        suffix = suffix_for_language(snippet.language)
        cfg = config_for_suffix(suffix)
        if cfg is None:
            continue
        path = _synthetic_path(source_key, snippet.sample_id, suffix)
        decls, aliases, raw = ast_engine.extract_file(
            path, cfg, snippet.code.encode("utf-8")
        )
        context_aliases: list[tuple[str, str, str | None]] = []
        if snippet.import_context:
            context_path = _synthetic_path(
                source_key, f"{snippet.sample_id}-context", suffix
            )
            _context_decls, context_aliases, _context_raw = ast_engine.extract_file(
                context_path, cfg, snippet.import_context.encode("utf-8")
            )
        parsed += 1
        merged = target_decls + decls
        resolved = ast_resolution.resolve_calls(
            merged,
            {path: list(aliases) + list(context_aliases)},
            raw,
            source_key,
            commit_sha,
            language=cfg.name,
            source_id=source_id,
            sample_prefix="",
            source_role=source_role,
            origin_kind=snippet.origin_kind or origin_kind,
            target_names=effective_names,
            target_prefixes=prefixes,
            source_target_status=source_status,
            strict_target_status=bool(filt.get("require_resolved_targets")),
            stdlib_filter_enabled=bool(
                getattr(settings, "stdlib_filter_enabled", True)
            ),
            stdlib_table_version=str(getattr(settings, "stdlib_table_version", "1")),
        )
        resolved = _relocate_snippet_calls(resolved, snippet)
        count, ids = _upsert_calls(
            conn,
            library_id,
            version_id,
            resolved,
            commit_sha,
            entity_version_id=entity_version_id,
        )
        calls += count
        wanted.update(ids)
    swept = _sweep_calls(conn, version_id, wanted, source_id=source_id)
    conn.commit()
    return {"parsed": parsed, "calls": calls, "swept": swept, "source_id": source_id}
