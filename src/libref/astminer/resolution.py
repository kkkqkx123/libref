"""Two-pass resolution: the first pass collects declarations and aliases; the second expands calls and assigns confidence."""

from __future__ import annotations

import posixpath
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

_TEST_DIR_NAMES = frozenset({"test", "tests", "testing", "__tests__"})


def resolve_calls(
    decls: list[ApiDeclRecord],
    file_aliases: dict[str, list[tuple[str, str]]],
    calls: list[RawCall],
    repo: str,
    commit_sha: str = "",
) -> list[CallInstanceRecord]:
    by_file: dict[str, set[str]] = {}
    parent_by_qual: dict[str, str | None] = {}
    params_by_qual: dict[str, list[str]] = {}
    param_types_by_qual: dict[str, dict[str, str]] = {}
    arity_by_qual: dict[str, int] = {}
    file_by_qual: dict[str, str] = {}
    for decl in decls:
        if decl.kind not in CALLABLE_DECL_KINDS:
            continue
        by_file.setdefault(decl.file, set()).add(decl.qualified_name)
        parent_by_qual.setdefault(decl.qualified_name, decl.parent)
        file_by_qual.setdefault(decl.qualified_name, decl.file)
        params = list(decl.params or [])
        params_by_qual.setdefault(decl.qualified_name, params)
        arity_by_qual.setdefault(decl.qualified_name, len(params))
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
    declared = set(parent_by_qual)
    alias_per_file = {path: dict(items) for path, items in file_aliases.items()}
    _close_reexports(alias_per_file)
    out: list[CallInstanceRecord] = []
    order_counter: dict[str, int] = {}
    ordered = sorted(calls, key=lambda c: (c.caller_file, c.caller or "", c.start_byte))
    for call in ordered:
        alias_map = alias_per_file.get(call.caller_file, {})
        same_file = by_file.get(call.caller_file, set())
        resolved, confidence = resolve_one(
            call.raw_callee,
            call.caller,
            declared,
            same_file,
            alias_map,
            parent_by_qual,
            caller_file=call.caller_file,
            file_by_qual=file_by_qual,
            params_by_qual=params_by_qual,
            param_types_by_qual=param_types_by_qual,
            arity_by_qual=arity_by_qual,
            argument_count=call.argument_count,
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
    caller_file: str | None = None,
    file_by_qual: dict[str, str] | None = None,
    params_by_qual: dict[str, list[str]] | None = None,
    param_types_by_qual: dict[str, dict[str, str]] | None = None,
    arity_by_qual: dict[str, int] | None = None,
    argument_count: int | None = None,
) -> tuple[str, Confidence]:
    if not raw:
        return raw, "AMBIGUOUS"
    head, _, _rest = raw.partition(".")
    if is_builtin_bare(raw, alias_map) and raw not in same_file:
        return raw, "AMBIGUOUS"
    if head in _SELF_HEADS and caller:
        base = caller.rpartition(".")[0] if "." in caller else caller
        resolved = (base + raw[len(head) :]) if base else raw
        if resolved in declared:
            return resolved, "EXTRACTED"
        return resolved, "AMBIGUOUS"
    if (
        "." not in raw
        and caller
        and params_by_qual is not None
        and _shadowed_by_param(raw, caller, parent_by_qual, params_by_qual)
    ):
        return raw, "AMBIGUOUS"
    if head in alias_map:
        resolved = alias_map[head] + raw[len(head) :]
        if resolved in declared:
            return resolved, "EXTRACTED"
        suffixed = [q for q in declared if q == resolved or q.endswith("." + resolved)]
        if len(suffixed) == 1 and suffixed[0] in same_file:
            return suffixed[0], "EXTRACTED"
        if _is_stdlib_target(resolved):
            return resolved, "AMBIGUOUS"
        raw_hits = [q for q in declared if q == raw or q.endswith(f".{raw}")]
        if len(raw_hits) == 1:
            if raw_hits[0] in same_file:
                return raw_hits[0], "EXTRACTED"
            return raw_hits[0], "INFERRED"
        return resolved, "INFERRED"
    if (
        "." in raw
        and head not in alias_map
        and caller
        and param_types_by_qual is not None
    ):
        typed = _resolve_via_receiver_type(
            raw, caller, parent_by_qual, param_types_by_qual, declared, same_file
        )
        if typed is not None:
            return typed
    candidates = [q for q in declared if q == raw or q.endswith(f".{raw}")]
    if not candidates:
        return raw, "AMBIGUOUS"
    same_hits = [q for q in candidates if q in same_file]
    if len(same_hits) == 1:
        return same_hits[0], "EXTRACTED"
    if len(same_hits) > 1:
        candidates = same_hits
    elif raw in same_file:
        return raw, "EXTRACTED"
    picked = _disambiguate(
        candidates,
        caller,
        parent_by_qual,
        caller_file=caller_file,
        file_by_qual=file_by_qual,
        arity_by_qual=arity_by_qual,
        argument_count=argument_count,
    )
    if picked is None:
        return raw, "AMBIGUOUS"
    if picked == raw or picked in same_file:
        return picked, "EXTRACTED"
    return picked, "INFERRED"


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


def _disambiguate(
    candidates: list[str],
    caller: str | None,
    parent_by_qual: dict[str, str | None] | None,
    *,
    caller_file: str | None = None,
    file_by_qual: dict[str, str] | None = None,
    arity_by_qual: dict[str, int] | None = None,
    argument_count: int | None = None,
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
    if argument_count is not None and arity_by_qual is not None and len(candidates) > 1:
        matched = [c for c in candidates if arity_by_qual.get(c) == argument_count]
        if len(matched) == 1:
            return matched[0]
        if matched:
            candidates = matched
    if caller_file is not None and file_by_qual is not None and len(candidates) > 1:
        files = [file_by_qual.get(c) for c in candidates]
        winner = _unique_candidate_by_affinity(caller_file, files)
        if winner is not None:
            return candidates[winner]
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
    suffixed = [q for q in declared if q == resolved or q.endswith("." + resolved)]
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


def _is_test_path(path: str | None) -> bool:
    if not path:
        return False
    norm = path.replace("\\", "/").lower()
    segments = [seg for seg in norm.split("/") if seg]
    if any(seg in _TEST_DIR_NAMES for seg in segments[:-1]):
        return True
    filename = segments[-1] if segments else ""
    stem, _, _ = filename.rpartition(".")
    check = stem or filename
    if check.startswith("test_") or check.endswith("_test") or check == "test":
        return True
    if filename.endswith((".test.js", ".spec.js")):
        return True
    return filename.endswith("test.java") or filename.startswith("test")


def _directory_segments(path: str) -> list[str]:
    norm = posixpath.normpath(path.replace("\\", "/").lstrip("/"))
    segments = [seg for seg in norm.split("/") if seg and seg != "."]
    return segments[:-1] if segments else []


def _common_prefix_len(left: list[str], right: list[str]) -> int:
    count = 0
    for a, b in zip(left, right):
        if a != b:
            break
        count += 1
    return count


def _unique_candidate_by_affinity(
    caller_file: str | None, candidate_paths: list[str | None]
) -> int | None:
    total = len(candidate_paths)
    if total == 0:
        return None
    if total == 1:
        return 0
    pool: list[int] = list(range(total))
    if caller_file:
        caller_dir = _directory_segments(caller_file)
        scores = [
            _common_prefix_len(caller_dir, _directory_segments(p)) if p else 0
            for p in candidate_paths
        ]
        best = max(scores)
        if best > 0:
            narrowed = [i for i in pool if scores[i] == best]
            if len(narrowed) == 1:
                return narrowed[0]
            pool = narrowed
    non_test = [i for i in pool if not _is_test_path(candidate_paths[i])]
    if non_test and len(non_test) < len(pool):
        if len(non_test) == 1:
            return non_test[0]
        pool = non_test
    if len(pool) == 1:
        return pool[0]
    return None
