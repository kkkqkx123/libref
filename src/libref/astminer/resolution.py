"""两遍解析：首遍收声明与别名，次遍展开调用并定置信。"""

from __future__ import annotations

from libref.astminer.models import ApiDeclRecord, CallInstanceRecord, RawCall
from libref.astminer.rules import CALLABLE_DECL_KINDS

_SELF_HEADS = frozenset({"self", "cls", "this"})


def resolve_calls(
    decls: list[ApiDeclRecord],
    file_aliases: dict[str, list[tuple[str, str]]],
    calls: list[RawCall],
    repo: str,
) -> list[CallInstanceRecord]:
    by_file: dict[str, set[str]] = {}
    params_by_qual: dict[str, int] = {}
    parent_by_qual: dict[str, str | None] = {}
    span_by_qual: dict[str, list[int]] = {}
    for decl in decls:
        if decl.kind not in CALLABLE_DECL_KINDS:
            continue
        by_file.setdefault(decl.file, set()).add(decl.qualified_name)
        params_by_qual.setdefault(decl.qualified_name, len(decl.params))
        parent_by_qual.setdefault(decl.qualified_name, decl.parent)
        span_by_qual.setdefault(decl.qualified_name, []).append(decl.start_byte)
    declared = set(params_by_qual)
    alias_per_file = {path: dict(items) for path, items in file_aliases.items()}
    out: list[CallInstanceRecord] = []
    order_counter: dict[str, int] = {}
    ordered = sorted(calls, key=lambda c: (c.caller_file, c.caller or "", c.start_byte))
    for call in ordered:
        alias_map = alias_per_file.get(call.caller_file, {})
        same_file = by_file.get(call.caller_file, set())
        resolved, confidence = resolve_one(
            call.raw_callee, call.receiver, call.caller, declared, same_file, alias_map,
            params_by_qual, parent_by_qual, span_by_qual, call.argument_count, call.start_byte,
        )
        scope = f"{call.caller_file}::{call.caller}" if call.caller else call.caller_file
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
            )
        )
    return out


def resolve_one(
    raw: str,
    receiver: str | None,
    caller: str | None,
    declared: set[str],
    same_file: set[str],
    alias_map: dict[str, str],
    params_by_qual: dict[str, int] | None = None,
    parent_by_qual: dict[str, str | None] | None = None,
    span_by_qual: dict[str, list[int]] | None = None,
    argument_count: int | None = None,
    call_byte: int | None = None,
) -> tuple[str, str]:
    if not raw:
        return raw, "AMBIGUOUS"
    head, _, _rest = raw.partition(".")
    if head in _SELF_HEADS and caller:
        base = caller.rpartition(".")[0] if "." in caller else caller
        resolved = (base + raw[len(head):]) if base else raw
        if resolved in declared:
            return resolved, "EXTRACTED"
        return resolved, "AMBIGUOUS"
    if head in alias_map:
        resolved = alias_map[head] + raw[len(head):]
        if resolved in declared:
            return resolved, "EXTRACTED"
        return resolved, "INFERRED"
    candidates = [q for q in declared if q == raw or q.endswith(f".{raw}")]
    if not candidates:
        return raw, "AMBIGUOUS"
    if raw in same_file:
        return raw, "EXTRACTED"
    picked = _disambiguate(
        candidates, caller, parent_by_qual, params_by_qual, span_by_qual,
        argument_count, call_byte, receiver,
    )
    if picked == raw or picked in same_file:
        return picked, "EXTRACTED"
    return picked, "AMBIGUOUS"


def _disambiguate(
    candidates: list[str],
    caller: str | None,
    parent_by_qual: dict[str, str | None] | None,
    params_by_qual: dict[str, int] | None,
    span_by_qual: dict[str, list[int]] | None,
    argument_count: int | None,
    call_byte: int | None,
    receiver: str | None,
) -> str:
    if len(candidates) == 1:
        return candidates[0]
    if caller and parent_by_qual:
        chain = _scope_chain(caller, parent_by_qual)
        scoped = [c for c in candidates if parent_by_qual.get(c) in chain]
        if len(scoped) == 1:
            return scoped[0]
        if scoped:
            candidates = scoped
    if receiver in _SELF_HEADS and caller:
        base = caller.rpartition(".")[0] if "." in caller else ""
        prefixed = [c for c in candidates if base and c.startswith(base + ".")]
        if len(prefixed) == 1:
            return prefixed[0]
        if prefixed:
            candidates = prefixed
    if argument_count is not None and params_by_qual:
        matched = [c for c in candidates if params_by_qual.get(c) == argument_count]
        if len(matched) == 1:
            return matched[0]
        if matched:
            candidates = matched
    if call_byte is not None and span_by_qual:
        best = min(
            candidates,
            key=lambda c: (
                min(abs(s - call_byte) for s in span_by_qual.get(c, [0])),
                c,
            ),
        )
        return best
    return sorted(candidates)[0]


def _scope_chain(
    caller: str, parent_by_qual: dict[str, str | None]
) -> set[str | None]:
    chain: set[str | None] = set()
    current: str | None = caller
    while current is not None:
        chain.add(current)
        current = parent_by_qual.get(current)
    chain.add(None)
    return chain
