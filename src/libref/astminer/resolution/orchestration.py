"""Two-pass call resolution entry points."""

from __future__ import annotations

from libref.astminer import stdlib as _stdlib
from libref.astminer.models import (
    TARGET_STATUS_EXTERNAL,
    TARGET_STATUS_PREFIX,
    TARGET_STATUS_RESOLVED,
    TARGET_STATUS_UNCONFIRMED,
    ApiDeclRecord,
    CallInstanceRecord,
    Confidence,
    RawCall,
)
from libref.astminer.resolution.candidates import _disambiguate, _filter_candidates
from libref.astminer.resolution.helpers import (
    _SELF_HEADS,
    _ancestor_list,
    _build_suffix_index,
    _is_stdlib_target,
    _language_for_file,
    _normalize_type_name,
    _strip_glued_new,
)
from libref.astminer.resolution.imports import (
    _close_alias_chains,
    _close_reexports,
    _expand_alias,
    _prefer_imported,
    _rewrite_factory_chain,
)
from libref.astminer.resolution.inheritance import (
    _resolve_owner_via_bases,
    _resolve_self_via_bases,
    _resolve_via_receiver_type,
    _shadowed_by_param,
)
from libref.astminer.rules import CALLABLE_DECL_KINDS, is_builtin_bare


def resolve_calls(
    decls: list[ApiDeclRecord],
    file_aliases: dict[str, list[tuple[str, str, str | None]]],
    calls: list[RawCall],
    repo: str,
    commit_sha: str = "",
    language: str | None = None,
    *,
    source_id: int | None = None,
    sample_prefix: str = "",
    source_role: str = "implementation",
    origin_kind: str = "repo_file",
    target_names: set[str] | frozenset[str] | None = None,
    target_prefixes: tuple[str, ...] | list[str] | None = None,
    stdlib_filter_enabled: bool = True,
    stdlib_table_version: str = _stdlib.STDLIB_TABLE_VERSION,
    source_target_status: str = "",
    strict_target_status: bool = False,
) -> list[CallInstanceRecord]:
    by_file: dict[str, set[str]] = {}
    parent_by_qual: dict[str, str | None] = {}
    argcounts_by_qual: dict[str, set[int]] = {}
    params_by_qual: dict[str, list[str]] = {}
    param_types_by_qual: dict[str, dict[str, str]] = {}
    bases_by_qual: dict[str, list[str]] = {}
    return_by_qual: dict[str, str] = {}
    for decl in decls:
        if decl.kind not in CALLABLE_DECL_KINDS:
            continue
        by_file.setdefault(decl.file, set()).add(decl.qualified_name)
        parent_by_qual.setdefault(decl.qualified_name, decl.parent)
        params = list(decl.params or [])
        argcounts_by_qual.setdefault(decl.qualified_name, set()).add(len(params))
        params_by_qual.setdefault(decl.qualified_name, params)
        types = dict(param_types_by_qual.setdefault(decl.qualified_name, {}))
        for idx, name in enumerate(params):
            if not name or name in types:
                continue
            raw_type = ""
            if idx < len(decl.param_types or []):
                raw_type = (decl.param_types or [])[idx] or ""
            if raw_type.strip():
                types[name] = raw_type
        param_types_by_qual[decl.qualified_name] = types
        if decl.bases:
            slot = bases_by_qual.setdefault(decl.qualified_name, [])
            for base in decl.bases:
                if base and base not in slot:
                    slot.append(base)
        owner = _normalize_type_name(decl.return_type or "")
        if owner and decl.qualified_name not in return_by_qual:
            return_by_qual[decl.qualified_name] = owner
    declared = set(parent_by_qual)
    suffix_index = _build_suffix_index(declared)
    import_per_file: dict[str, dict[str, str]] = {}
    vars_per_file: dict[str, list[tuple[str, str, str]]] = {}
    for path, items in file_aliases.items():
        imports: dict[str, str] = {}
        scoped: list[tuple[str, str, str]] = []
        for alias, full, scope in items:
            if not alias or not full:
                continue
            if scope is None:
                imports[alias] = full
            else:
                scoped.append((alias, full, scope))
        import_per_file[path] = imports
        vars_per_file[path] = scoped
    for _ in range(len(import_per_file) + 2):
        snapshot = {path: dict(mapping) for path, mapping in import_per_file.items()}
        _close_reexports(import_per_file)
        _close_alias_chains(import_per_file)
        if import_per_file == snapshot:
            break
    effective_cache: dict[tuple[str, str], dict[str, str]] = {}

    def effective_for(path: str, caller: str | None) -> dict[str, str]:
        key = (path, caller or "")
        hit = effective_cache.get(key)
        if hit is not None:
            return hit
        merged = dict(import_per_file.get(path, {}))
        varlist = vars_per_file.get(path, [])
        if varlist:
            ancestors = _ancestor_list(caller, parent_by_qual) if caller else []
            chain_set = set(ancestors)
            if caller and "." in caller:
                chain_set.add(caller.rpartition(".")[0])
            module_vars: dict[str, str] = {}
            ranked: list[tuple[int, str, str]] = []
            for var, full, scope in varlist:
                if scope == "":
                    module_vars[var] = full
                elif scope in chain_set:
                    try:
                        rank = ancestors.index(scope)
                    except ValueError:
                        rank = len(ancestors)
                    ranked.append((rank, var, full))
            merged.update(module_vars)
            for _rank, var, full in sorted(ranked, reverse=True):
                merged[var] = full
            closed = {path: merged}
            _close_alias_chains(closed)
            merged = closed[path]
        effective_cache[key] = merged
        return merged

    out: list[CallInstanceRecord] = []
    order_counter: dict[str, int] = {}
    ordered = sorted(calls, key=lambda c: (c.caller_file, c.caller or "", c.start_byte))
    for call in ordered:
        alias_map = effective_for(call.caller_file, call.caller)
        same_file = by_file.get(call.caller_file, set())
        resolved, confidence, resolve_kind = resolve_one(
            call.raw_callee,
            call.caller,
            declared,
            same_file,
            alias_map,
            parent_by_qual,
            language=language or _language_for_file(call.caller_file),
            params_by_qual=params_by_qual,
            param_types_by_qual=param_types_by_qual,
            suffix_index=suffix_index,
            bases_by_qual=bases_by_qual,
            return_by_qual=return_by_qual,
            argcounts_by_qual=argcounts_by_qual,
            receiver=call.receiver,
            argument_count=call.argument_count,
            relation_kind=call.relation_kind,
            macro_generated=call.macro_generated,
            target_names=target_names,
            stdlib_filter_enabled=stdlib_filter_enabled,
            stdlib_table_version=stdlib_table_version,
        )
        scope = (
            f"{call.caller_file}::{call.caller}" if call.caller else call.caller_file
        )
        order_counter[scope] = order_counter.get(scope, 0) + 1
        prefixes = tuple(target_prefixes or ())
        names = frozenset(target_names or ())
        prefix_hit = bool(prefixes) and resolved.startswith(prefixes)
        name_hit = bool(names) and resolved in names
        if source_role == "implementation":
            in_target = True
        elif names or prefixes:
            in_target = name_hit or prefix_hit
        else:
            in_target = False
        if source_target_status == TARGET_STATUS_UNCONFIRMED:
            target_status = TARGET_STATUS_UNCONFIRMED
        elif not in_target:
            target_status = TARGET_STATUS_EXTERNAL
        elif source_role == "implementation" or (
            name_hit
            and confidence != "AMBIGUOUS"
            and resolve_kind not in ("unresolved", "stdlib", "builtin")
        ):
            target_status = TARGET_STATUS_RESOLVED
        elif strict_target_status:
            target_status = TARGET_STATUS_PREFIX
        else:
            target_status = TARGET_STATUS_RESOLVED
        base_sample = (
            f"{call.caller_file}::{call.caller}"
            if call.caller
            else str(call.caller_file or "sample")
        )
        sample = f"{sample_prefix}:{base_sample}" if sample_prefix else base_sample
        out.append(
            CallInstanceRecord(
                callee=resolved,
                raw_callee=call.raw_callee,
                args=list(call.args),
                caller_file=call.caller_file,
                caller=call.caller,
                line_start=call.line_start,
                line_end=call.line_end,
                start_byte=call.start_byte,
                end_byte=call.end_byte,
                receiver=call.receiver,
                func=call.func,
                argument_count=call.argument_count,
                call_order=order_counter[scope],
                confidence=confidence,
                resolve_kind=resolve_kind,
                repo=repo,
                commit_sha=commit_sha,
                source_id=source_id,
                sample_id=sample,
                source_role=source_role,
                origin_kind=origin_kind,
                in_target_api=in_target,
                target_status=target_status,
                relation_kind=call.relation_kind,
                macro_generated=call.macro_generated,
                macro_host=call.macro_host,
                macro_host_file=call.macro_host_file,
                macro_definition_line=call.macro_definition_line,
                macro_nesting=call.macro_nesting,
                macro_conditions=list(call.macro_conditions),
                macro_body_kind=call.macro_body_kind,
                macro_body_signature=call.macro_body_signature,
            )
        )
    return out


def resolve_one(
    raw: str,
    caller: str | None,
    declared: set[str],
    same_file: set[str],
    alias_map: dict[str, str],
    parent_by_qual: dict[str, str | None] | None = None,
    *,
    language: str | None = None,
    params_by_qual: dict[str, list[str]] | None = None,
    param_types_by_qual: dict[str, dict[str, str]] | None = None,
    suffix_index: dict[str, list[str]] | None = None,
    bases_by_qual: dict[str, list[str]] | None = None,
    return_by_qual: dict[str, str] | None = None,
    argcounts_by_qual: dict[str, set[int]] | None = None,
    receiver: str | None = None,
    argument_count: int | None = None,
    relation_kind: str = "direct",
    macro_generated: bool = False,
    target_names: set[str] | frozenset[str] | None = None,
    stdlib_filter_enabled: bool = True,
    stdlib_table_version: str = _stdlib.STDLIB_TABLE_VERSION,
) -> tuple[str, Confidence, str]:
    """Resolve one call, then apply stdlib and macro-shape classification."""
    result = _resolve_one_core(
        raw,
        caller,
        declared,
        same_file,
        alias_map,
        parent_by_qual,
        language=language,
        params_by_qual=params_by_qual,
        param_types_by_qual=param_types_by_qual,
        suffix_index=suffix_index,
        bases_by_qual=bases_by_qual,
        return_by_qual=return_by_qual,
        argcounts_by_qual=argcounts_by_qual,
        receiver=receiver,
        argument_count=argument_count,
        target_names=target_names,
    )
    resolved, confidence, kind = result
    # A target API with a stdlib-looking name always wins over classification.
    protected = resolved in declared or resolved in (target_names or ())
    if (
        not protected
        and stdlib_filter_enabled
        and kind != "stdlib"
        and _stdlib.is_stdlib(resolved or raw, language, stdlib_table_version)
    ):
        return resolved, "INFERRED", "stdlib"
    if (macro_generated or relation_kind == "macro") and confidence == "EXTRACTED":
        confidence = "INFERRED"
    return resolved, confidence, kind


def _resolve_one_core(
    raw: str,
    caller: str | None,
    declared: set[str],
    same_file: set[str],
    alias_map: dict[str, str],
    parent_by_qual: dict[str, str | None] | None = None,
    *,
    language: str | None = None,
    params_by_qual: dict[str, list[str]] | None = None,
    param_types_by_qual: dict[str, dict[str, str]] | None = None,
    suffix_index: dict[str, list[str]] | None = None,
    bases_by_qual: dict[str, list[str]] | None = None,
    return_by_qual: dict[str, str] | None = None,
    argcounts_by_qual: dict[str, set[int]] | None = None,
    receiver: str | None = None,
    argument_count: int | None = None,
    target_names: set[str] | frozenset[str] | None = None,
) -> tuple[str, Confidence, str]:
    if not raw:
        return raw, "AMBIGUOUS", "unresolved"
    raw = raw.replace("::", ".").replace("->", ".")
    index = suffix_index if suffix_index is not None else _build_suffix_index(declared)
    raw = _strip_glued_new(raw, alias_map, index)
    head, _, _rest = raw.partition(".")
    if is_builtin_bare(raw, alias_map, language) and raw not in same_file:
        # A known builtin/global has exactly one identity, so the reference is
        # not ambiguous; it is only inferred in the sense that the target is
        # platform-owned and absent from this repository.
        return raw, "INFERRED", "builtin"
    if (
        "." not in raw
        and caller
        and params_by_qual is not None
        and _shadowed_by_param(raw, caller, parent_by_qual, params_by_qual)
    ):
        return raw, "AMBIGUOUS", "param-shadowed"
    expanded = _expand_alias(raw, alias_map)
    if expanded is not None:
        resolved = expanded
        if resolved in declared:
            return resolved, "EXTRACTED", "alias-direct"
        suffixed = index.get(resolved, [])
        if len(suffixed) == 1 and suffixed[0] in same_file:
            return suffixed[0], "EXTRACTED", "alias-suffix"
        if (
            _is_stdlib_target(resolved, language)
            and resolved not in declared
            and resolved not in (target_names or ())
        ):
            # Stdlib targets are named exactly once; mining drops them via
            # the source column unless a reviewer approves them. A target
            # API with the same name is resolved as a normal declared call.
            return resolved, "INFERRED", "stdlib"
        rewritten = _rewrite_factory_chain(
            resolved, declared, index, same_file, return_by_qual or {}
        )
        if rewritten is not None and rewritten != resolved:
            resolved = rewritten
            if resolved in declared:
                return resolved, "EXTRACTED", "factory"
            suffixed = index.get(resolved, [])
            if len(suffixed) == 1:
                if suffixed[0] in same_file:
                    return suffixed[0], "EXTRACTED", "factory"
                return suffixed[0], "INFERRED", "factory"
        inherited = _resolve_owner_via_bases(
            resolved, declared, bases_by_qual or {}, index, same_file
        )
        if inherited is not None:
            return inherited, "EXTRACTED", "inherited"
        raw_hits = index.get(raw, [])
        if len(raw_hits) == 1:
            if raw_hits[0] in same_file:
                return raw_hits[0], "EXTRACTED", "suffix"
            return raw_hits[0], "INFERRED", "suffix"
        if not index.get(expanded):
            return resolved, "INFERRED", "alias-external"
        return resolved, "INFERRED", "alias-direct"
    if head in _SELF_HEADS and caller:
        base = caller.rpartition(".")[0] if "." in caller else caller
        resolved = (base + raw[len(head) :]) if base else raw
        if resolved in declared:
            return resolved, "EXTRACTED", "self-direct"
        inherited = _resolve_self_via_bases(
            base, raw[len(head) :], declared, bases_by_qual or {}, index, same_file
        )
        if inherited is not None:
            return inherited, "EXTRACTED", "self-inherited"
        return resolved, "AMBIGUOUS", "unresolved"
    if (
        "." in raw
        and head not in alias_map
        and caller
        and param_types_by_qual is not None
    ):
        typed = _resolve_via_receiver_type(
            raw,
            caller,
            parent_by_qual,
            param_types_by_qual,
            declared,
            same_file,
            index,
            bases_by_qual,
        )
        if typed is not None:
            return typed[0], typed[1], "receiver-type"
    candidates = list(index.get(raw, []))
    if not candidates:
        return raw, "AMBIGUOUS", "unresolved"
    candidates = _filter_candidates(
        candidates,
        receiver,
        argument_count,
        argcounts_by_qual
        or (
            {q: {len(params_by_qual.get(q, []))} for q in candidates}
            if params_by_qual
            else None
        ),
        alias_map,
        parent_by_qual,
        raw,
    )
    if not candidates:
        return raw, "AMBIGUOUS", "unresolved"
    same_hits = [q for q in candidates if q in same_file]
    if len(same_hits) == 1:
        return same_hits[0], "EXTRACTED", "same-file"
    if len(same_hits) > 1:
        candidates = same_hits
    elif raw in same_file:
        return raw, "EXTRACTED", "same-file"
    picked = _disambiguate(candidates, caller, parent_by_qual)
    if picked is None:
        preferred = _prefer_imported(candidates, alias_map)
        if preferred is not None:
            return preferred, "INFERRED", "import-preferred"
        return raw, "AMBIGUOUS", "unresolved"
    if len(candidates) == 1:
        if picked == raw or picked in same_file:
            return picked, "EXTRACTED", "suffix"
        return picked, "INFERRED", "suffix"
    if picked == raw or picked in same_file:
        return picked, "EXTRACTED", "scope"
    return picked, "INFERRED", "scope"
