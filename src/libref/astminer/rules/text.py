"""Shared text shaping over tree-sitter nodes."""

from __future__ import annotations

from typing import Any

MAX_SIGNATURE_LEN = 500

_signature_missing_count = 0


def signature_missing_count() -> int:
    return _signature_missing_count


def compose_signature(
    items,
    source: bytes,
    main_start: int,
    main_end: int,
    body_start: int | None = None,
    expect_body: bool = False,
) -> str:
    global _signature_missing_count
    from libref.astminer import queries as _queries

    parts = sorted(
        (item for item in items if _queries.is_signature_capture(item.name)),
        key=lambda item: item.start_byte,
    )
    composed = " ".join(item.text for item in parts if item.text)
    if composed:
        if len(composed) > MAX_SIGNATURE_LEN:
            return composed[:MAX_SIGNATURE_LEN]
        return composed
    if body_start is not None and main_start < body_start < main_end:
        header = source[main_start:body_start].decode("utf-8", "replace").strip()
        if header:
            return header
        return ""
    if expect_body:
        _signature_missing_count += 1
        return ""
    fallback = source[main_start:main_end].decode("utf-8", "replace").strip()
    if len(fallback) > MAX_SIGNATURE_LEN:
        fallback = fallback[:MAX_SIGNATURE_LEN]
    return fallback


# Only genuine call nodes. Element tags describe rendering, constructor
# delegation carries no callee, and goroutine or deferred wrappers are
# covered by the plain call patterns on the inner expression.
CALL_NODE_TYPES = frozenset(
    {
        "call",
        "call_expression",
        "function_call",
        "juxt_function_call",
        "method_invocation",
        "new_expression",
        "object_creation_expression",
        "method_reference",
        "macro_invocation",
        "invocation_expression",
    }
)


_PYTHON_BARE_CALLS = frozenset(
    {
        "str",
        "int",
        "float",
        "bool",
        "list",
        "dict",
        "set",
        "tuple",
        "len",
        "range",
        "print",
        "open",
        "isinstance",
        "type",
        "super",
    }
)

_JAVASCRIPT_BARE_CALLS = frozenset(
    {
        "String",
        "Number",
        "Boolean",
        "Object",
        "Array",
        "Promise",
        "Map",
        "Set",
        "JSON",
        "Math",
        "console",
    }
)

_KOTLIN_BARE_CALLS = frozenset(
    {
        "listOf",
        "mutableListOf",
        "mapOf",
        "mutableMapOf",
        "setOf",
        "mutableSetOf",
        "arrayOf",
        "sequenceOf",
        "println",
        "print",
        "error",
        "lazy",
    }
)

_GROOVY_BARE_CALLS = frozenset(
    {
        "println",
        "print",
        "printf",
        "sprintf",
    }
)

BUILTIN_BARE_CALLS = (
    _PYTHON_BARE_CALLS
    | _JAVASCRIPT_BARE_CALLS
    | _KOTLIN_BARE_CALLS
    | _GROOVY_BARE_CALLS
)

BUILTIN_BARE_BY_LANGUAGE: dict[str, frozenset[str]] = {
    "python": _PYTHON_BARE_CALLS,
    "javascript": _JAVASCRIPT_BARE_CALLS,
    "typescript": _JAVASCRIPT_BARE_CALLS,
    "kotlin": _KOTLIN_BARE_CALLS,
    "groovy": _GROOVY_BARE_CALLS,
}


def is_builtin_bare(
    raw: str, alias_map: dict[str, str], language: str | None = None
) -> bool:
    head = raw.partition(".")[0]
    table = (
        BUILTIN_BARE_BY_LANGUAGE.get(language, BUILTIN_BARE_CALLS)
        if language is not None
        else BUILTIN_BARE_CALLS
    )
    return bool(head) and head in table and head not in alias_map


def slice_text(node: Any, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def generic_dotted(node: Any, source: bytes) -> str:
    obj = None
    for field in ("object", "value", "receiver"):
        obj = node.child_by_field_name(field)
        if obj is not None:
            break
    name_node = None
    for field in ("attribute", "field", "name", "property"):
        name_node = node.child_by_field_name(field)
        if name_node is not None:
            break
    if obj is not None and name_node is not None:
        return f"{generic_dotted(obj, source)}.{slice_text(name_node, source).strip()}"
    if node.type in ("dotted_name", "scoped_identifier"):
        names: list[str] = []
        for child in node.children:
            if child.type == "identifier":
                text = slice_text(child, source).strip()
                if text:
                    names.append(text)
            elif child.type in ("dotted_name", "scoped_identifier"):
                nested = generic_dotted(child, source)
                if nested:
                    names.append(nested)
        if names:
            return ".".join(names)
    if node.type == "generic_name":
        for child in node.children:
            if child.type == "identifier":
                text = slice_text(child, source).strip()
                if text:
                    return text
        return ""
    if node.type == "identifier":
        return slice_text(node, source).strip()
    return slice_text(node, source).replace(" ", "").strip()


def _is_literal(node: Any) -> bool:
    kind = node.type
    return any(
        part in kind
        for part in (
            "string",
            "literal",
            "integer",
            "float",
            "number",
            "true",
            "false",
            "none",
            "null",
        )
    )


def _is_container(node: Any) -> bool:
    if node.type in (
        "list",
        "tuple",
        "dictionary",
        "set",
        "list_pattern",
        "array_initializer",
    ):
        return True
    return "initializer" in node.type and "list" not in node.type


def _is_splat(node: Any) -> bool:
    return "splat" in node.type


def summarize_arg(node: Any, source: bytes) -> str:
    if node.type == "argument":
        for child in node.children:
            if child.is_named:
                return summarize_arg(child, source)
        return "?"
    if node.type == "keyword_argument":
        name_node = node.child_by_field_name("name")
        value_node = node.child_by_field_name("value")
        name = slice_text(name_node, source).strip() if name_node is not None else ""
        prefix = f"{name}=" if name else "kw="
        if value_node is None:
            return prefix
        value = summarize_arg(value_node, source)
        if value == "?":
            return prefix
        return f"{prefix}{value}"[:64]
    if _is_literal(node):
        return slice_text(node, source).strip().replace("\n", " ")[:48]
    if node.type == "identifier":
        return slice_text(node, source).strip()[:48]
    if node.type in (
        "attribute",
        "dotted_name",
        "field_access",
        "scoped_identifier",
    ):
        return generic_dotted(node, source)[:48]
    if _is_container(node):
        return "[...]"
    if _is_splat(node):
        return slice_text(node, source).strip().replace("\n", " ")[:48]
    if node.type in CALL_NODE_TYPES:
        fn = node.child_by_field_name("function")
        if fn is None:
            fn = node.child_by_field_name("name")
        if fn is not None:
            return f"{generic_dotted(fn, source)[:32]}(...)"
        return "(...)"
    return "?"


def summarize_arguments(args_node: Any, source: bytes) -> list[str]:
    out: list[str] = []
    for child in args_node.children:
        if not child.is_named:
            continue
        out.append(summarize_arg(child, source))
    return out


def count_arguments(args_node: Any) -> int:
    return sum(1 for child in args_node.children if child.is_named)


def param_name(pnode: Any, source: bytes) -> str:
    if pnode.type in ("identifier", "simple_identifier"):
        return slice_text(pnode, source).strip()
    named = pnode.child_by_field_name("name")
    if named is not None:
        return slice_text(named, source).strip()
    for child in pnode.children:
        if child.type in ("identifier", "simple_identifier"):
            return slice_text(child, source).strip()
    for child in pnode.children:
        if child.is_named:
            text = param_name(child, source)
            if text:
                return text
    return ""


def params_from_params_node(params_node: Any, source: bytes) -> list[str]:
    names: list[str] = []
    for param in params_node.children:
        if not param.is_named:
            continue
        name = param_name(param, source)
        if name:
            names.append(name)
    return names


def type_text_of_param(pnode: Any, source: bytes) -> str:
    for child in pnode.children:
        if child.type == "type_annotation":
            for sub in child.children:
                if sub.is_named:
                    text = slice_text(sub, source).strip()
                    if text:
                        return text[:128]
            text = slice_text(child, source).lstrip(":").strip()
            if text:
                return text[:128]
        if child.type == "type":
            text = slice_text(child, source).strip()
            if text:
                return text[:128]
        if child.type in ("type_identifier", "generic_type", "scoped_type_identifier"):
            text = slice_text(child, source).strip()
            if text:
                return text[:128]
    for child in pnode.children:
        if not child.is_named:
            continue
        if child.type in (
            "identifier",
            "list_splat_pattern",
            "dictionary_splat_pattern",
        ):
            continue
        text = type_text_of_param(child, source)
        if text:
            return text
    typed = pnode.child_by_field_name("type")
    if typed is not None:
        text = slice_text(typed, source).strip()
        if text:
            return text[:128]
    return ""


def param_types_from_params_node(params_node: Any, source: bytes) -> list[str]:
    types: list[str] = []
    for param in params_node.children:
        if not param.is_named:
            continue
        name = param_name(param, source)
        if not name:
            continue
        types.append(type_text_of_param(param, source))
    return types


def shaped_return(node: Any | None, source: bytes) -> str:
    if node is None:
        return ""
    return slice_text(node, source).strip().lstrip(":").strip()[:128]


def normalize_owner_text(text: str) -> str:
    inner = (text or "").strip()
    while len(inner) >= 2 and inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1].strip()
    if not inner:
        return ""
    last = inner.split()[-1]
    last = last.lstrip("*&?!~").strip()
    for opener in ("<", "(", "["):
        if opener in last:
            last = last.split(opener, 1)[0].strip()
    last = last.strip("()<>[]").strip()
    if not last or " " in last:
        return ""
    return last.split(".")[-1].strip() or ""
