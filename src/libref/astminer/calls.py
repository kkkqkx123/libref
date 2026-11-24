"""Call extraction and caller attribution."""

from __future__ import annotations

from libref.astminer import queries as _queries
from libref.astminer import rules as _rules
from libref.astminer.macro_calls import (
    find_macro_calls,
)
from libref.astminer.models import ApiDeclRecord, RawCall
from libref.astminer.ownership import _RELATION_PRIORITY, _relation_kind
from libref.astminer.parse import _merge_matches, get_parser


def _dedupe_calls(calls: list[RawCall]) -> list[RawCall]:
    """Collapse overlapping hits on the same call site from several patterns."""
    kept: list[RawCall] = []
    ordered = sorted(calls, key=lambda c: (c.caller_file, c.caller or "", c.start_byte))
    for call in ordered:
        tail = call.func or call.raw_callee.rpartition(".")[2]
        size = call.end_byte - call.start_byte
        merged = False
        for index, other in enumerate(kept):
            if other.caller_file != call.caller_file or other.caller != call.caller:
                continue
            other_tail = other.func or other.raw_callee.rpartition(".")[2]
            if tail != other_tail:
                continue
            other_size = other.end_byte - other.start_byte
            larger = max(size, other_size)
            smaller = min(size, other_size)
            if larger > smaller * 2 or smaller == 0:
                continue
            overlap = min(call.end_byte, other.end_byte) - max(
                call.start_byte, other.start_byte
            )
            if overlap < smaller * 0.8:
                continue
            rank = _RELATION_PRIORITY.get(call.relation_kind, 0)
            other_rank = _RELATION_PRIORITY.get(other.relation_kind, 0)
            if (rank, -size) > (other_rank, -other_size):
                kept[index] = call
            merged = True
            break
        if not merged:
            kept.append(call)
    return kept


def _build_calls(path, cfg, source, call_matches, decls, root=None) -> list[RawCall]:
    groups = _merge_matches(call_matches, source)
    calls: list[RawCall] = []
    for node, items in groups:
        shapes = [_queries.is_call_capture(item.name) for item in items]
        if node.type not in _rules.CALL_NODE_TYPES and not any(shapes):
            continue
        if _is_module_loader(node, source):
            continue
        raw, receiver, func = _rules.extract_call_parts(items, node, source)
        if not raw:
            continue
        relation = _relation_kind(items, node, source)
        if relation != "macro" and cfg.name in ("c", "cpp"):
            macro_names = {
                decl.qualified_name.rpartition(".")[2]
                for decl in decls
                if decl.kind == "macro"
            }
            if (func or raw).rpartition(".")[2] in macro_names:
                relation = "macro"
        args_node = _rules.first_node(items, ".arguments")
        if args_node is None:
            args_node = node.child_by_field_name("arguments")
        if args_node is None:
            for child in node.children:
                if child.type in ("argument_list", "arguments", "token_tree"):
                    args_node = child
                    break
        args = (
            _rules.summarize_arguments(args_node, source)
            if args_node is not None
            and args_node.type in ("argument_list", "arguments")
            else []
        )
        count = (
            _rules.count_arguments(args_node)
            if args_node is not None
            and args_node.type in ("argument_list", "arguments")
            else 0
        )
        calls.append(
            RawCall(
                raw_callee=raw,
                args=args,
                caller_file=path,
                caller=_caller_for(node, decls),
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                receiver=receiver,
                func=func,
                argument_count=count,
                relation_kind=relation,
            )
        )
    if root is not None:
        for macro in find_macro_calls(root, source, cfg.name, path, get_parser(cfg)):
            calls.append(
                RawCall(
                    raw_callee=macro.name,
                    args=list(macro.args),
                    caller_file=path,
                    caller=_caller_at(decls, macro.start_byte),
                    line_start=macro.line_start,
                    line_end=macro.line_end,
                    start_byte=macro.start_byte,
                    end_byte=macro.end_byte,
                    func=macro.name,
                    relation_kind="macro",
                    macro_generated=True,
                    macro_host=macro.host_macro,
                    macro_host_file=macro.host_file or path,
                    macro_definition_line=macro.macro_definition_line,
                    macro_nesting=macro.macro_nesting,
                    macro_conditions=list(macro.macro_conditions),
                    macro_body_kind=macro.macro_body_kind,
                    macro_body_signature=macro.macro_body_signature,
                )
            )
    return _dedupe_calls(calls)


def _is_module_loader(node, source: bytes) -> bool:
    if node.type != "call_expression":
        return False
    fn = node.child_by_field_name("function")
    if fn is None:
        return False
    if fn.type == "import":
        return True
    return (
        fn.type == "identifier" and _rules.slice_text(fn, source).strip() == "require"
    )


def _caller_for(node, decls: list[ApiDeclRecord]) -> str | None:
    return _caller_at(decls, node.start_byte, node.end_byte)


def _caller_at(
    decls: list[ApiDeclRecord], start: int, end: int | None = None
) -> str | None:
    best: ApiDeclRecord | None = None
    for decl in decls:
        if decl.kind not in ("function", "method", "constructor"):
            continue
        if not (decl.start_byte <= start and (end is None or end <= decl.end_byte)):
            continue
        if best is None or (
            decl.start_byte >= best.start_byte and decl.end_byte <= best.end_byte
        ):
            best = decl
    return best.qualified_name if best is not None else None
