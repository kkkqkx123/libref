"""两遍解析：首遍收声明与别名，次遍展开调用并定置信。"""

from __future__ import annotations

from libref.astminer.models import ApiDeclRecord, CallInstanceRecord, RawCall


def resolve_calls(
    decls: list[ApiDeclRecord],
    aliases: list[tuple[str, str]],
    calls: list[RawCall],
    repo: str,
) -> list[CallInstanceRecord]:
    declared = {d.qualified_name for d in decls}
    leaf_names = {d.qualified_name.split(".")[-1] for d in decls}
    alias_map = dict(aliases)
    out: list[CallInstanceRecord] = []
    for call in calls:
        resolved, confidence = resolve_one(
            call.raw_callee, call.caller, declared, leaf_names, alias_map
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
    leaf_names: set[str],
    alias_map: dict[str, str],
) -> tuple[str, str]:
    if not raw:
        return raw, "AMBIGUOUS"
    head, _, _rest = raw.partition(".")
    if head in ("self", "cls") and caller:
        base = caller.rpartition(".")[0] if "." in (caller or "") else caller
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
    if head in leaf_names or raw.split(".")[-1] in leaf_names:
        return raw, "AMBIGUOUS"
    return raw, "AMBIGUOUS"
