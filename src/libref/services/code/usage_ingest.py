"""Ingestion of documentation and reference snippets as usage evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

from libref.astminer.code_blocks import import_context_from_text, suffix_for_language
from libref.astminer.config import LANGUAGE_CONFIGS
from libref.astminer.language_evidence import LanguageFacts, resolve_language
from libref.astminer.models import (
    SNIPPET_STATUS_UNLABELED,
    SNIPPET_STATUS_UNSUPPORTED,
    TARGET_STATUS_RESOLVED,
    ApiDeclRecord,
    CodeSnippet,
    ParseEvidence,
)
from libref.astminer.snippet_status import classify_snippet_status
from libref.services.code import snippet_records
from libref.services.code.repo_persist import _sweep_calls, _upsert_calls
from libref.services.code.usage_sources import register_source, source_target_status

_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_CANDIDATES = 4
_EVIDENCE_CACHE: OrderedDict[tuple[str, str, int], ParseEvidence] = OrderedDict()
_EVIDENCE_CACHE_CAP = 2048


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
    exclude_nonpublic: bool = False,
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
    hidden = re.compile(r"(^|/)(?:examples?|tests?|benchmarks?)(/|$)", re.IGNORECASE)
    for row in rows:
        file_path = str(row[2] or "").replace("\\", "/")
        if exclude_nonpublic and hidden.search(file_path):
            continue
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


def _code_hash(code: str) -> str:
    return hashlib.sha256(" ".join(str(code or "").split()).encode("utf-8")).hexdigest()


def _config_for_language(language: str):
    for cfg in LANGUAGE_CONFIGS.values():
        if cfg.name == language:
            return cfg
    return None


def _public_aliases(
    conn,
    entity_version_id: int,
    library: str,
    target_names: set[str],
    tenant_id: str,
) -> dict[str, str]:
    """Resolve source-declared public names to their internal declarations."""
    from libref.services.infra.tenant import tenant_clause

    aliases: dict[str, str] = {}
    export_clause, export_params = tenant_clause(conn, "api_export_aliases", tenant_id)
    rows = conn.execute(
        "SELECT alias, target, kind FROM api_export_aliases WHERE version_id=?"
        + (" AND " + export_clause if export_clause else ""),
        (int(entity_version_id), *export_params),
    ).fetchall()
    import_clause, import_params = tenant_clause(conn, "import_edges", tenant_id)
    imported = conn.execute(
        "SELECT alias, target FROM import_edges WHERE version_id=?"
        + (" AND " + import_clause if import_clause else "")
        + " ORDER BY length(importer_file), importer_file",
        (int(entity_version_id), *import_params),
    ).fetchall()
    entity_clause, entity_params = tenant_clause(conn, "api_entities", tenant_id)
    parents = {
        str(row[0]): str(row[1] or "")
        for row in conn.execute(
            "SELECT qualified_name, parent FROM api_entities WHERE version_id=?"
            + (" AND " + entity_clause if entity_clause else ""),
            (int(entity_version_id), *entity_params),
        ).fetchall()
    }
    for alias, target in imported:
        aliases.setdefault(str(alias), str(target))
    wildcard: list[tuple[str, str]] = []
    for alias, target, kind in rows:
        if str(alias).endswith(".*"):
            wildcard.append((str(alias)[:-2], str(target)))
        elif str(target) in target_names:
            aliases[str(alias)] = str(target)
    module_names = {name for name in target_names if not parents.get(name)}
    exported: dict[str, set[str]] = {}
    for prefix, target_module in wildcard:
        exported.setdefault(prefix, set()).add(target_module)

    def modules_reachable(module: str, seen: set[str]) -> set[str]:
        if module in seen:
            return set()
        found = {module}
        for child in exported.get(module, ()):
            found.update(modules_reachable(child, seen | {module}))
        return found

    for prefix, target_module in wildcard:
        for concrete in modules_reachable(target_module, set()):
            for name in module_names:
                head, _, member = name.rpartition(".")
                if (head == concrete or head.startswith(concrete + ".")) and member:
                    aliases.setdefault(f"{prefix}.{member}", name)
    for short, module in imported:
        for concrete in modules_reachable(str(module), set()):
            for name in module_names:
                head, _, member = name.rpartition(".")
                if (head == concrete or head.startswith(concrete + ".")) and member:
                    aliases.setdefault(f"{short}.{member}", name)
    root = re.sub(r"[^a-z0-9]+", ".", str(library).lower()).strip(".")
    if root:
        for name in module_names:
            head, _, member = name.rpartition(".")
            if member:
                aliases.setdefault(f"{root}.{member}", name)
    for alias, target in list(aliases.items()):
        current = target
        seen = {alias}
        for _ in range(8):
            parts = current.split(".")
            stepped = current
            for length in range(len(parts) - 1, 0, -1):
                prefix = ".".join(parts[:length])
                if prefix in aliases and prefix not in seen:
                    stepped = (
                        aliases[prefix] + "." + ".".join(parts[length:])
                        if length < len(parts)
                        else aliases[prefix]
                    )
                    seen.add(prefix)
                    break
            if stepped == current or stepped.split(".")[0] in seen:
                break
            current = stepped
        aliases[alias] = current
    return aliases


def _cached_evidence(code: str, cfg) -> ParseEvidence:
    """Bounded parse cache keyed by snippet digest and language config."""
    from libref.astminer import engine as ast_engine

    key = (
        hashlib.sha256(code.encode("utf-8")).hexdigest(),
        cfg.name,
        cfg.config_version,
    )
    cached = _EVIDENCE_CACHE.get(key)
    if cached is not None:
        _EVIDENCE_CACHE.move_to_end(key)
        return cached
    _decls, _aliases, _calls, evidence = ast_engine.extract_file_with_evidence(
        "snippet", cfg, code.encode("utf-8")
    )
    _EVIDENCE_CACHE[key] = evidence
    _EVIDENCE_CACHE.move_to_end(key)
    while len(_EVIDENCE_CACHE) > _EVIDENCE_CACHE_CAP:
        _EVIDENCE_CACHE.popitem(last=False)
    return evidence


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
    snippet_candidates: list[str] | None = None,
    snippet_language_rules: dict[str, str] | None = None,
) -> dict[str, int]:
    """Parse snippets with target API facts and persist source-aware records."""
    from libref.astminer import engine as ast_engine
    from libref.astminer import resolution as ast_resolution
    from libref.services.registry.libraries import LibraryService

    catalog = LibraryService(conn)
    library_id, version_id = catalog.ensure_library_version(
        library, version, ecosystem, tenant_id=tenant_id
    )
    options: dict[str, Any]
    if snippet_candidates is not None or snippet_language_rules is not None:
        snippet_records.record_source_options(
            conn,
            version_id,
            candidates=snippet_candidates or (),
            rules=snippet_language_rules,
            tenant_id=tenant_id,
        )
        options = {
            "candidates": list(snippet_candidates or ()),
            "rules": dict(snippet_language_rules or {}),
        }
    else:
        options = snippet_records.load_source_options(conn, version_id, tenant_id)
    confirmed = snippet_records.load_confirmed_languages(
        conn, version_id, source_key, tenant_id
    )
    source_selection = dict(selection or {})
    if options["candidates"]:
        source_selection["snippet_languages"] = options["candidates"]
    source_id = register_source(
        conn,
        version_id=version_id,
        source_key=source_key,
        source_role=source_role,
        origin_kind=origin_kind,
        source_url=source_url,
        commit_sha=commit_sha,
        target_filter=target_filter or {},
        selection=source_selection,
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
        exclude_nonpublic=source_role == "doc_snippet",
    )
    target_names = {decl.qualified_name for decl in target_decls}
    public_aliases = (
        _public_aliases(conn, entity_version_id, library, target_names, tenant_id)
        if source_role == "doc_snippet"
        else {}
    )
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
    counts: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for snippet in snippets:
        digest = _code_hash(snippet.code)
        decision = resolve_language(
            LanguageFacts(
                url=snippet.url or source_url,
                selector=snippet.origin_kind,
                manual_rules=options["rules"],
            )
        )
        confirmed_language = confirmed.get(snippet.sample_id) or ""
        language = confirmed_language
        raw_language = snippet.raw_language
        language_source = snippet.language_source
        language_rule = snippet.language_rule
        if not language and (snippet.language or raw_language):
            language = snippet.language or raw_language
            language_source = language_source or "block_tag"
        if not language and decision.is_explicit:
            language = decision.language
            raw_language = raw_language or decision.language
            language_source = decision.source
            language_rule = decision.rule
        attempts: list[dict[str, Any]] = []
        usable: list[str] = []
        evidence: ParseEvidence | None = None
        if not language:
            candidates = list(snippet.candidates or options["candidates"])
            for candidate in candidates[:_MAX_CANDIDATES]:
                cfg = _config_for_language(candidate)
                if cfg is None:
                    continue
                evidence = _cached_evidence(snippet.code, cfg)
                attempts.append(
                    {
                        "language": cfg.name,
                        "call_count": evidence.call_count,
                        "decl_count": evidence.decl_count,
                        "has_error": evidence.has_error,
                        "usable": evidence.has_api_evidence,
                    }
                )
                if evidence.has_api_evidence:
                    usable.append(cfg.name)
            if len(usable) == 1:
                language = usable[0]
                raw_language = raw_language or language
                language_source = "candidate"
            elif len(usable) > 1:
                raw_language = "+".join(usable)
                language_source = "candidate"
        cfg = _config_for_language(language) if language else None
        if language_source == "candidate" and len(usable) != 1:
            status = classify_snippet_status(
                None,
                language=language,
                raw_language=raw_language,
                language_source=language_source,
                usable_candidates=len(usable),
            )
            evidence = None
        elif cfg is None:
            status = (
                SNIPPET_STATUS_UNSUPPORTED if raw_language else SNIPPET_STATUS_UNLABELED
            )
            evidence = None
        else:
            path = _synthetic_path(
                source_key, snippet.sample_id, suffix_for_language(language)
            )
            decls, aliases, raw = ast_engine.extract_file(
                path, cfg, snippet.code.encode("utf-8")
            )
            context_aliases: list[tuple[str, str, str | None]] = []
            context_text = "\n".join(
                part
                for part in (
                    import_context_from_text(snippet.code),
                    snippet.import_context,
                )
                if part
            )
            if context_text:
                context_path = _synthetic_path(
                    source_key,
                    f"{snippet.sample_id}-context",
                    suffix_for_language(language),
                )
                _context_decls, context_aliases, _context_raw = ast_engine.extract_file(
                    context_path, cfg, context_text.encode("utf-8")
                )
            parsed += 1
            merged = target_decls + decls
            resolved = ast_resolution.resolve_calls(
                merged,
                {
                    path: list(aliases)
                    + list(context_aliases)
                    + [
                        (alias, target, None)
                        for alias, target in public_aliases.items()
                    ]
                },
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
                stdlib_table_version=str(
                    getattr(settings, "stdlib_table_version", "1")
                ),
            )
            resolved = _relocate_snippet_calls(resolved, snippet)
            target_calls = sum(
                1
                for call in resolved
                if call.in_target_api and call.target_status == TARGET_STATUS_RESOLVED
            )
            evidence = _cached_evidence(snippet.code, cfg)
            status = classify_snippet_status(
                evidence,
                language=language,
                raw_language=raw_language,
                language_source=language_source,
                configured=True,
                usable_candidates=1 if language_source == "candidate" else 0,
                target_calls=target_calls,
            )
            dedupe_key = (cfg.name, " ".join(snippet.code.split()))
            if dedupe_key not in seen:
                seen.add(dedupe_key)
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
        counts[status] = counts.get(status, 0) + 1
        snippet_records.upsert_snippet_record(
            conn,
            {
                "version_id": version_id,
                "source_key": source_key,
                "snippet_id": snippet.sample_id,
                "code_hash": digest,
                "code": snippet.code,
                "url": snippet.url or source_url,
                "heading": snippet.heading,
                "ordinal": snippet.ordinal,
                "origin_kind": snippet.origin_kind or origin_kind,
                "origin_location": snippet.origin_location,
                "origin_start_byte": snippet.origin_start_byte,
                "origin_end_byte": snippet.origin_end_byte,
                "origin_start_line": snippet.origin_start_line,
                "origin_end_line": snippet.origin_end_line,
                "raw_language": raw_language,
                "language": language,
                "language_source": language_source,
                "language_rule": language_rule,
                "parse_status": status,
                "attempts": json.dumps(attempts, ensure_ascii=False),
                "source_id": source_id,
                "tenant_id": tenant_id,
            },
        )
    swept = _sweep_calls(conn, version_id, wanted, source_id=source_id)
    conn.commit()
    return {
        "snippets": sum(counts.values()),
        "parsed": parsed,
        "calls": calls,
        "swept": swept,
        "source_id": source_id,
        **counts,
    }
