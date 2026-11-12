"""Two-pass resolution: the first pass collects declarations and aliases; the second expands calls and assigns confidence."""

from __future__ import annotations

import sys

from libref.astminer.models import (
    ApiDeclRecord,
    CallInstanceRecord,
    Confidence,
    RawCall,
)
from libref.astminer.rules import CALLABLE_DECL_KINDS, is_builtin_bare

_SELF_HEADS = frozenset({"self", "cls", "this", "Self"})

try:
    _PYTHON_STDLIB = set(sys.stdlib_module_names)
except AttributeError:  # pragma: no cover - fallback for older interpreters
    _PYTHON_STDLIB = {
        "os",
        "sys",
        "json",
        "re",
        "pathlib",
        "typing",
        "collections",
        "itertools",
        "functools",
        "math",
        "datetime",
        "logging",
        "unittest",
        "argparse",
        "io",
        "shutil",
        "glob",
        "pickle",
        "copy",
        "enum",
        "dataclasses",
        "abc",
        "inspect",
        "importlib",
        "hashlib",
        "sqlite3",
        "asyncio",
        "threading",
        "subprocess",
        "urllib",
        "http",
    }

_JAVA_STDLIB_PREFIXES = ("java.", "javax.", "jdk.", "sun.", "com.sun.")
_RUST_STDLIB_PREFIXES = ("std.", "core.", "alloc.")
_CSHARP_STDLIB_PREFIXES = ("System.",)
_KOTLIN_STDLIB_PREFIXES = ("kotlin.", "kotlinx.")
_GROOVY_STDLIB_PREFIXES = ("groovy.", "org.codehaus.groovy.")
_NODE_BUILTINS = frozenset(
    {
        "fs",
        "path",
        "os",
        "util",
        "events",
        "stream",
        "http",
        "https",
        "url",
        "querystring",
        "assert",
        "buffer",
        "child_process",
        "cluster",
        "crypto",
        "dgram",
        "dns",
        "http2",
        "module",
        "net",
        "process",
        "readline",
        "string_decoder",
        "timers",
        "tls",
        "tty",
        "v8",
        "vm",
        "worker_threads",
        "zlib",
    }
)


def _language_for_file(path: str | None) -> str | None:
    if not path:
        return None
    from pathlib import Path as _Path

    from libref.astminer import config as _config

    cfg = _config.config_for_suffix(_Path(path).suffix)
    return cfg.name if cfg is not None else None


def _build_suffix_index(declared: set[str]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for qual in declared:
        slot = index.setdefault(qual, [])
        if qual not in slot:
            slot.append(qual)
        parts = qual.split(".")
        for start in range(1, len(parts)):
            suffix = ".".join(parts[start:])
            if not suffix:
                continue
            bucket = index.setdefault(suffix, [])
            if qual not in bucket:
                bucket.append(qual)
    for key, value in index.items():
        index[key] = sorted(value)
    return index


def resolve_calls(
    decls: list[ApiDeclRecord],
    file_aliases: dict[str, list[tuple[str, str]]],
    calls: list[RawCall],
    repo: str,
    commit_sha: str = "",
    language: str | None = None,
) -> list[CallInstanceRecord]:
    by_file: dict[str, set[str]] = {}
    parent_by_qual: dict[str, str | None] = {}
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
    alias_per_file = {path: dict(items) for path, items in file_aliases.items()}
    for _ in range(len(alias_per_file) + 2):
        snapshot = {path: dict(mapping) for path, mapping in alias_per_file.items()}
        _close_reexports(alias_per_file)
        _close_alias_chains(alias_per_file)
        if alias_per_file == snapshot:
            break
    out: list[CallInstanceRecord] = []
    order_counter: dict[str, int] = {}
    ordered = sorted(calls, key=lambda c: (c.caller_file, c.caller or "", c.start_byte))
    for call in ordered:
        alias_map = alias_per_file.get(call.caller_file, {})
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
        )
        scope = (
            f"{call.caller_file}::{call.caller}" if call.caller else call.caller_file
        )
        order_counter[scope] = order_counter.get(scope, 0) + 1
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
) -> tuple[str, Confidence, str]:
    if not raw:
        return raw, "AMBIGUOUS", "unresolved"
    index = suffix_index if suffix_index is not None else _build_suffix_index(declared)
    head, _, _rest = raw.partition(".")
    if is_builtin_bare(raw, alias_map, language) and raw not in same_file:
        return raw, "AMBIGUOUS", "builtin"
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
        if _is_stdlib_target(resolved):
            return resolved, "AMBIGUOUS", "stdlib"
        rewritten = _rewrite_factory_head(
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
        raw_hits = index.get(raw, [])
        if len(raw_hits) == 1:
            if raw_hits[0] in same_file:
                return raw_hits[0], "EXTRACTED", "suffix"
            return raw_hits[0], "INFERRED", "suffix"
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
        )
        if typed is not None:
            return typed[0], typed[1], "receiver-type"
    candidates = list(index.get(raw, []))
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
        return raw, "AMBIGUOUS", "unresolved"
    if len(candidates) == 1:
        if picked == raw or picked in same_file:
            return picked, "EXTRACTED", "suffix"
        return picked, "INFERRED", "suffix"
    if picked == raw or picked in same_file:
        return picked, "EXTRACTED", "scope"
    return picked, "INFERRED", "scope"


def _close_reexports(alias_per_file: dict[str, dict[str, str]]) -> None:
    package_exports: dict[str, str] = {}
    for path, mapping in alias_per_file.items():
        norm = path.replace("\\", "/")
        if norm.endswith("/__init__.py"):
            package = norm[: -len("/__init__.py")].replace("/", ".")
        elif norm == "__init__.py":
            package = ""
        else:
            continue
        for alias, full in list(mapping.items()):
            key = f"{package}.{alias}" if package else alias
            if key not in package_exports:
                package_exports[key] = full
    if not package_exports:
        return
    changed = True
    while changed:
        changed = False
        for mapping in alias_per_file.values():
            for alias, full in list(mapping.items()):
                target = package_exports.get(full)
                if target is not None and target != full:
                    mapping[alias] = target
                    changed = True


def _close_alias_chains(alias_per_file: dict[str, dict[str, str]]) -> None:
    for mapping in alias_per_file.values():
        if len(mapping) < 2:
            continue
        snapshot = dict(mapping)
        for alias, full in snapshot.items():
            if not full:
                continue
            seen = {alias}
            current = full
            for _ in range(len(snapshot) + 1):
                head = current.partition(".")[0] if current else ""
                if not head or head not in snapshot or head in seen:
                    break
                target = snapshot[head]
                if not target:
                    break
                if "." in current:
                    _, _, rest = current.partition(".")
                    current = target + "." + rest if target else current
                else:
                    current = target
                if current.partition(".")[0] == alias:
                    current = full
                    break
                seen.add(head)
            if current != full and current.partition(".")[0] != alias:
                mapping[alias] = current


def _expand_alias(raw: str, alias_map: dict[str, str]) -> str | None:
    if not raw or not alias_map:
        return None
    parts = raw.split(".")
    for length in range(len(parts), 0, -1):
        prefix = ".".join(parts[:length])
        target = alias_map.get(prefix)
        if target:
            if length == len(parts):
                return target
            return target + "." + ".".join(parts[length:])
    return None


def _owner_options(
    name: str,
    declared: set[str],
    suffix_index: dict[str, list[str]],
    same_file: set[str],
) -> list[str]:
    if name in declared:
        return [name]
    options = suffix_index.get(name, [])
    if len(options) == 1:
        return options
    same = [q for q in options if q in same_file]
    if len(same) == 1:
        return same
    return []


def _rewrite_factory_head(
    resolved: str,
    declared: set[str],
    suffix_index: dict[str, list[str]],
    same_file: set[str],
    return_by_qual: dict[str, str],
) -> str | None:
    if not resolved or "." not in resolved or not return_by_qual:
        return None
    parts = resolved.split(".")
    for length in range(len(parts) - 1, 0, -1):
        func_name = ".".join(parts[:length])
        method_rest = ".".join(parts[length:])
        if not method_rest:
            continue
        funcs = _owner_options(func_name, declared, suffix_index, same_file)
        if len(funcs) != 1:
            continue
        owner_name = (return_by_qual.get(funcs[0]) or "").strip()
        if not owner_name:
            continue
        owners = _owner_options(owner_name, declared, suffix_index, same_file)
        if len(owners) != 1:
            continue
        return owners[0] + "." + method_rest
    return None


def _resolve_self_via_bases(
    class_qual: str | None,
    suffix: str,
    declared: set[str],
    bases_by_qual: dict[str, list[str]],
    suffix_index: dict[str, list[str]],
    same_file: set[str],
) -> str | None:
    if not class_qual or not suffix:
        return None
    seen: set[str] = {class_qual}
    frontier: list[str] = [class_qual]
    while frontier:
        next_frontier: list[str] = []
        level_hits: list[str] = []
        for current in frontier:
            for raw_base in bases_by_qual.get(current, []):
                cleaned = _normalize_type_name(raw_base)
                if not cleaned:
                    continue
                options = suffix_index.get(cleaned, [])
                if not options:
                    continue
                if len(options) == 1:
                    base_options = options
                else:
                    same = [q for q in options if q in same_file]
                    if len(same) != 1:
                        continue
                    base_options = same
                for base_qual in base_options:
                    if base_qual in seen:
                        continue
                    seen.add(base_qual)
                    hit = base_qual + suffix
                    if hit in declared and hit not in level_hits:
                        level_hits.append(hit)
                    next_frontier.append(base_qual)
        if len(set(level_hits)) == 1:
            return level_hits[0]
        if level_hits:
            return None
        frontier = next_frontier
    return None


def _disambiguate(
    candidates: list[str],
    caller: str | None,
    parent_by_qual: dict[str, str | None] | None,
) -> str | None:
    if len(candidates) == 1:
        return candidates[0]
    if caller and parent_by_qual:
        chain = _scope_chain(caller, parent_by_qual)
        scoped = [c for c in candidates if parent_by_qual.get(c) in chain]
        if len(scoped) == 1:
            return scoped[0]
        if scoped:
            candidates = scoped
            if len(candidates) == 1:
                return candidates[0]
    return None


def _scope_chain(caller: str, parent_by_qual: dict[str, str | None]) -> set[str | None]:
    chain: set[str | None] = set()
    current: str | None = caller
    while current is not None:
        chain.add(current)
        current = parent_by_qual.get(current)
    chain.add(None)
    return chain


def _shadowed_by_param(
    raw: str,
    caller: str,
    parent_by_qual: dict[str, str | None] | None,
    params_by_qual: dict[str, list[str]],
) -> bool:
    current: str | None = caller
    seen: set[str] = set()
    while current is not None and current not in seen:
        seen.add(current)
        if raw in (params_by_qual.get(current) or []):
            return True
        current = parent_by_qual.get(current) if parent_by_qual else None
    return False


def _lookup_param_type(
    name: str,
    caller: str,
    parent_by_qual: dict[str, str | None] | None,
    param_types_by_qual: dict[str, dict[str, str]],
) -> str:
    current: str | None = caller
    seen: set[str] = set()
    while current is not None and current not in seen:
        seen.add(current)
        hit = (param_types_by_qual.get(current) or {}).get(name)
        if hit and hit.strip():
            return hit
        current = parent_by_qual.get(current) if parent_by_qual else None
    return ""


def _normalize_type_name(raw_type: str) -> str:
    text = raw_type.strip().strip("\"'")
    if not text:
        return ""
    text = text.split("|")[0].strip()
    text = text.removeprefix("...").removesuffix("...").strip()
    text = text.lstrip("*&?").strip()
    for opener, _ in (("[", "]"), ("<", ">"), ("(", ")")):
        if opener in text:
            text = text.split(opener, 1)[0].strip()
            break
    text = text.strip().strip("\"'")
    if not text or " " in text:
        return ""
    return text


def _resolve_via_receiver_type(
    raw: str,
    caller: str,
    parent_by_qual: dict[str, str | None] | None,
    param_types_by_qual: dict[str, dict[str, str]],
    declared: set[str],
    same_file: set[str],
    suffix_index: dict[str, list[str]] | None = None,
) -> tuple[str, Confidence] | None:
    head, _, rest = raw.partition(".")
    if not head or not rest:
        return None
    annotation = _lookup_param_type(head, caller, parent_by_qual, param_types_by_qual)
    owner = _normalize_type_name(annotation)
    if not owner:
        return None
    resolved = owner + raw[len(head) :]
    if resolved in declared:
        return resolved, "EXTRACTED"
    index = suffix_index if suffix_index is not None else _build_suffix_index(declared)
    suffixed = index.get(resolved, [])
    if len(suffixed) == 1:
        if suffixed[0] in same_file:
            return suffixed[0], "EXTRACTED"
        return suffixed[0], "INFERRED"
    return None


def _is_stdlib_target(resolved: str) -> bool:
    if not resolved:
        return False
    if resolved.startswith("node:"):
        return True
    head = resolved.partition(".")[0]
    if not head:
        return False
    if head in _PYTHON_STDLIB:
        return True
    if head in _NODE_BUILTINS:
        return True
    for prefix in _JAVA_STDLIB_PREFIXES:
        if resolved.startswith(prefix):
            return True
    for prefix in _RUST_STDLIB_PREFIXES + _CSHARP_STDLIB_PREFIXES:
        if resolved.startswith(prefix):
            return True
    for prefix in _KOTLIN_STDLIB_PREFIXES + _GROOVY_STDLIB_PREFIXES:
        if resolved.startswith(prefix):
            return True
    return False
