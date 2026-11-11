"""Entity selection over capture names."""

from __future__ import annotations

from typing import Any

from libref.astminer.rules.text import generic_dotted, slice_text

CALLABLE_DECL_KINDS = frozenset(
    {
        "class",
        "function",
        "method",
        "interface",
        "enum",
        "constructor",
        "record",
        "annotation",
        "struct",
        "union",
        "type",
        "macro",
    }
)


_ENTITY_KIND_BY_ROOT = {
    "entity.class": "class",
    "entity.function": "function",
    "entity.generator": "function",
    "entity.arrow": "function",
    "entity.object_method": "function",
    "entity.method": "method",
    "entity.constructor": "constructor",
    "entity.interface": "interface",
    "entity.enum": "enum",
    "entity.enum_constant": "enum_constant",
    "entity.record": "record",
    "entity.struct": "struct",
    "entity.union": "union",
    "entity.type": "type",
    "entity.macro": "macro",
    "entity.annotation": "annotation",
    "entity.field": "field",
    "entity.variable": "variable",
    "entity.decorator": "decorator",
    "entity.decorator.call": "decorator",
    "entity.package": "package",
    "entity.module": "module",
    "entity.lambda": "lambda",
}


_KIND_PRIORITY = ("method", "function", "constructor")


def entity_kind(root_captures: set[str]) -> str | None:
    for root in root_captures:
        if root in _ENTITY_KIND_BY_ROOT:
            return _ENTITY_KIND_BY_ROOT[root]
    return None


def refine_kind(kinds: set[str]) -> str | None:
    for want in _KIND_PRIORITY:
        if want in kinds:
            return want
    if len(kinds) == 1:
        return next(iter(kinds))
    return min(kinds) if kinds else None


def first_node(captures: dict[str, list], *suffixes: str) -> Any | None:
    for suffix in suffixes:
        best: Any | None = None
        best_key: tuple[int, int] | None = None
        for name, nodes in captures.items():
            if name.count(".") < 2 or not name.endswith(suffix) or not nodes:
                continue
            specificity = name.count(".")
            for node in nodes:
                span = node.end_byte - node.start_byte
                key = (specificity, -span)
                if best_key is None or key > best_key:
                    best_key = key
                    best = node
        if best is not None:
            return best
    return None


def all_texts(captures: dict[str, list], source: bytes, *suffixes: str) -> list[str]:
    out: list[str] = []
    for suffix in suffixes:
        for name, nodes in captures.items():
            if name.endswith(suffix):
                for node in nodes:
                    out.append(generic_dotted(node, source).strip())
    return [text for text in out if text]


def collect_simple_var_aliases(root: Any, source: bytes) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "assignment":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is not None and right is not None and left.type == "identifier":
                var = slice_text(left, source).strip()
                target = _call_target_name(right, source)
                if var and target:
                    found.append((var, target))
        elif node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            value_node = node.child_by_field_name("value")
            if (
                name_node is not None
                and value_node is not None
                and name_node.type == "identifier"
            ):
                var = slice_text(name_node, source).strip()
                target = _call_target_name(value_node, source)
                if var and target:
                    found.append((var, target))
        stack.extend(node.children)
    return found


def _call_target_name(node: Any, source: bytes) -> str:
    if node.type not in (
        "call",
        "call_expression",
        "new_expression",
        "object_creation_expression",
    ):
        return ""
    fn = node.child_by_field_name("function")
    if fn is None:
        fn = node.child_by_field_name("name")
    if fn is None:
        fn = node.child_by_field_name("constructor")
    if fn is None:
        fn = node.child_by_field_name("type")
    if fn is None:
        for child in node.children:
            if child.type in (
                "identifier",
                "attribute",
                "dotted_name",
                "scoped_identifier",
                "type_identifier",
            ):
                fn = child
                break
    if fn is None:
        return ""
    if fn.type == "import":
        return ""
    text = generic_dotted(fn, source).strip()[:128]
    if text in ("require", "import"):
        return ""
    return text


def _split_aliased(node: Any, source: bytes) -> tuple[str, str]:
    original = ""
    alias = ""
    for sub in node.children:
        if (
            sub.type in ("dotted_name", "scoped_identifier", "identifier")
            and not original
        ):
            original = generic_dotted(sub, source).strip()
        elif sub.type == "identifier" and original:
            alias = slice_text(sub, source).strip()
    return original, alias
