"""Ingestion of documentation and reference snippets as usage evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

from libref.astminer.code_blocks import suffix_for_language
from libref.astminer.config import config_for_suffix
from libref.astminer.models import ApiDeclRecord, CodeSnippet
from libref.services.code.repo_persist import _sweep_calls, _upsert_calls
from libref.services.code.usage_sources import register_source

_KIND_SUFFIXES = ("function", "method", "class", "interface", "enum", "constructor")
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _target_decls(conn, version_id: int) -> list[ApiDeclRecord]:
    """Load target API facts; usage sources never define the target surface."""
    rows = conn.execute(
        "SELECT qualified_name, kind, file, line_start, line_end, params,"
        " param_types, return_type, bases, parent, signature, has_doc"
        " FROM api_entities WHERE version_id=?",
        (version_id,),
    ).fetchall()
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
        tenant_id=tenant_id,
    )
    target_decls = _target_decls(conn, version_id)
    target_names = {decl.qualified_name for decl in target_decls}
    filt = target_filter or {}
    explicit_names = {str(v) for v in filt.get("names", []) if str(v).strip()}
    prefixes = tuple(str(v) for v in filt.get("prefixes", []) if str(v).strip())
    wanted: set[str] = set()
    calls = 0
    parsed = 0
    for snippet in snippets:
        suffix = suffix_for_language(snippet.language)
        cfg = config_for_suffix(suffix)
        if cfg is None:
            continue
        path = _synthetic_path(source_key, snippet.sample_id, suffix)
        decls, aliases, raw = ast_engine.extract_file(
            path, cfg, snippet.code.encode("utf-8")
        )
        parsed += 1
        merged = target_decls + decls
        resolved = ast_resolution.resolve_calls(
            merged,
            {path: aliases},
            raw,
            source_key,
            commit_sha,
            language=cfg.name,
            source_id=source_id,
            sample_prefix=snippet.sample_id,
            source_role=source_role,
            origin_kind=snippet.origin_kind or origin_kind,
            target_names=explicit_names or target_names,
            target_prefixes=prefixes,
            stdlib_filter_enabled=bool(
                getattr(settings, "stdlib_filter_enabled", True)
            ),
            stdlib_table_version=str(getattr(settings, "stdlib_table_version", "1")),
        )
        resolved = _relocate_snippet_calls(resolved, snippet)
        count, ids = _upsert_calls(conn, library_id, version_id, resolved, commit_sha)
        calls += count
        wanted.update(ids)
    swept = _sweep_calls(conn, version_id, wanted, source_id=source_id)
    conn.commit()
    return {"parsed": parsed, "calls": calls, "swept": swept, "source_id": source_id}


def library_source_names(conn, version_id: int) -> set[str]:
    """Qualified names of the target library surface for one version."""
    return {
        str(r[0])
        for r in conn.execute(
            "SELECT qualified_name FROM api_entities WHERE version_id=?", (version_id,)
        ).fetchall()
    }


def entity_kind_suffixes() -> tuple[str, ...]:
    """Callable entity kinds used when matching resolved calls to definitions."""
    return _KIND_SUFFIXES
