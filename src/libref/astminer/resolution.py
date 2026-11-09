"""Two-pass resolution: the first pass collects declarations and aliases; the second expands calls and assigns confidence."""

from __future__ import annotations

from libref.astminer.models import ApiDeclRecord, CallInstanceRecord, RawCall
from libref.astminer.rules import CALLABLE_DECL_KINDS

_SELF_HEADS = frozenset({"self", "cls", "this"})


def resolve_calls(
    decls: list[ApiDeclRecord],
    file_aliases: dict[str, list[tuple[str, str]]],
    calls: list[RawCall],
    repo: str,
    commit_sha: str = "",
) -> list[CallInstanceRecord]:
    by_file: dict[str, set[str]] = {}
    parent_by_qual: dict[str, str | None] = {}
    for decl in decls:
        if decl.kind not in CALLABLE_DECL_KINDS:
            continue
        by_file.setdefault(decl.file, set()).add(decl.qualified_name)
        parent_by_qual.setdefault(decl.qualified_name, decl.parent)
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
) -> tuple[str, str]:
    if not raw:
        return raw, "AMBIGUOUS"
    head, _, _rest = raw.partition(".")
    if head in _SELF_HEADS and caller:
        base = caller.rpartition(".")[0] if "." in caller else caller
        resolved = (base + raw[len(head) :]) if base else raw
        if resolved in declared:
            return resolved, "EXTRACTED"
        return resolved, "AMBIGUOUS"
    if head in alias_map:
        resolved = alias_map[head] + raw[len(head) :]
        if resolved in declared:
            return resolved, "EXTRACTED"
        suffixed = [q for q in declared if q == resolved or q.endswith("." + resolved)]
        if len(suffixed) == 1 and suffixed[0] in same_file:
            return suffixed[0], "EXTRACTED"
        return resolved, "INFERRED"
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
    picked = _disambiguate(candidates, caller, parent_by_qual)
    if picked is None:
        return raw, "AMBIGUOUS"
    if picked == raw or picked in same_file:
        return picked, "EXTRACTED"
    return picked, "AMBIGUOUS"


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
) -> str | None:
    if len(candidates) == 1:
        return candidates[0]
    if caller and parent_by_qual:
        chain = _scope_chain(caller, parent_by_qual)
        scoped = [c for c in candidates if parent_by_qual.get(c) in chain]
        if len(scoped) == 1:
            return scoped[0]
    return None


def _scope_chain(caller: str, parent_by_qual: dict[str, str | None]) -> set[str | None]:
    chain: set[str | None] = set()
    current: str | None = caller
    while current is not None:
        chain.add(current)
        current = parent_by_qual.get(current)
    chain.add(None)
    return chain
