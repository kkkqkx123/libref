"""两遍解析：首遍收声明与别名，次遍展开调用并定置信。"""

from __future__ import annotations

from libref.astminer.models import ApiDeclRecord, CallInstanceRecord, RawCall

_SELF_HEADS = frozenset({"self", "cls", "this"})


def resolve_calls(
    decls: list[ApiDeclRecord],
    file_aliases: dict[str, list[tuple[str, str]]],
    calls: list[RawCall],
    repo: str,
) -> list[CallInstanceRecord]:
    declared = {d.qualified_name for d in decls}
    alias_per_file = {path: dict(items) for path, items in file_aliases.items()}
    out: list[CallInstanceRecord] = []
    for call in calls:
        alias_map = alias_per_file.get(call.caller_file, {})
        resolved, confidence = resolve_one(
            call.raw_callee, call.caller, declared, alias_map
        )
        out.append(
            CallInstanceRecord(
                callee=resolved,
                raw_callee=call.raw_callee,
                args=list(call.args),
                caller_file=call.caller_file,
                caller=call.caller,
                line_start=call.line_start,
                line_end=call.line_end,
                confidence=confidence,
                repo=repo,
            )
        )
    return out


def resolve_one(
    raw: str,
    caller: str | None,
    declared: set[str],
    alias_map: dict[str, str],
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
    if raw in declared:
        return raw, "EXTRACTED"
    return raw, "AMBIGUOUS"
