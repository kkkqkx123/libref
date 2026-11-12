"""Capture conversion and entity selection over capture names."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from libref.astminer import queries as _queries
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

METHOD_OWNER_KINDS = frozenset(
    {"class", "interface", "enum", "record", "annotation", "struct", "union"}
)

_BODY_OWNERS = frozenset(
    {
        "entity.function",
        "entity.method",
        "entity.constructor",
        "entity.class",
        "entity.interface",
        "entity.struct",
        "entity.enum",
        "entity.union",
    }
)


@dataclass
class CapturedItem:
    """One capture with text sliced once at conversion time."""

    name: str
    text: str
    start_byte: int
    end_byte: int
    node: Any = None


def to_items(captures: dict[str, list], source: bytes) -> list[CapturedItem]:
    out: list[CapturedItem] = []
    for name, nodes in captures.items():
        for node in nodes:
            out.append(
                CapturedItem(
                    name=name,
                    text=slice_text(node, source).strip(),
                    start_byte=node.start_byte,
                    end_byte=node.end_byte,
                    node=node,
                )
            )
    return out


def is_phantom(node: Any) -> bool:
    if node.end_byte < node.start_byte:
        return True
    try:
        start_point = node.start_point
        end_point = node.end_point
    except AttributeError:
        return False
    return end_point < start_point


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


def roots_expect_body(roots: set[str]) -> bool:
    return any(root in _BODY_OWNERS for root in roots)


def first_node(items: list[CapturedItem], *suffixes: str) -> Any | None:
    for suffix in suffixes:
        best: CapturedItem | None = None
        best_key: tuple[int, int] | None = None
        for item in items:
            if len(_queries.capture_parts(item.name)) < 3:
                continue
            if not _queries.has_suffix(item.name, suffix):
                continue
            specificity = len(_queries.capture_parts(item.name))
            span = item.end_byte - item.start_byte
            key = (specificity, -span)
            if best_key is None or key > best_key:
                best_key = key
                best = item
        if best is not None:
            return best.node
    return None


def handled_entity_roots() -> set[str]:
    return set(_ENTITY_KIND_BY_ROOT) | {"entity.impl"}


def classify_entity_kind(
    roots: set[str], node: Any | None = None, parent_kind: str | None = None
) -> str | None:
    kinds = {
        kind for kind in (entity_kind({root}) for root in roots) if kind is not None
    }
    kind = refine_kind(kinds)
    if kind is None:
        return None
    if kind == "class" and node is not None:
        for child in node.children:
            if child.type == "interface":
                return "interface"
            if child.type == "enum":
                return "enum"
    if kind == "function" and parent_kind in METHOD_OWNER_KINDS:
        return "method"
    return kind


def root_names(items: list[CapturedItem]) -> set[str]:
    return {item.name for item in items if _queries.is_entity_root(item.name)}


def _name_from_root(items: list[CapturedItem], roots: set[str]) -> str:
    for root in roots:
        want = f"{root}.name"
        for item in items:
            if item.name == want and item.text:
                return item.text
    return ""


def _name_from_signature(items: list[CapturedItem], roots: set[str]) -> str:
    del roots
    for item in items:
        if _queries.has_suffix(item.name, ".signature.name") and item.text:
            return item.text
    return ""


def _name_from_suffix(items: list[CapturedItem], roots: set[str]) -> str:
    del roots
    for item in items:
        if _queries.has_suffix(item.name, ".name") and item.text:
            return item.text
    return ""


_NAME_RULES: tuple[Callable[[list[CapturedItem], set[str]], str], ...] = (
    _name_from_root,
    _name_from_signature,
    _name_from_suffix,
)


def extract_entity_name(items: list[CapturedItem], roots: set[str]) -> str:
    for build in _NAME_RULES:
        name = build(items, roots)
        if name:
            return name
    return ""


def extract_bases(items: list[CapturedItem]) -> list[str]:
    bases: list[str] = []
    for suffix in (".signature.base", ".base"):
        for item in items:
            if (
                _queries.has_suffix(item.name, suffix)
                and item.text
                and item.text not in bases
            ):
                bases.append(item.text)
    return bases


def _compose_call(receiver: str | None, func: str) -> tuple[str, str | None, str]:
    if receiver and func:
        return f"{receiver}.{func}", receiver, func
    if func and receiver is not None and "." in func:
        head, _, _ = func.rpartition(".")
        return func, head or None, func
    if func and receiver is None and "." in func:
        head, _, tail = func.rpartition(".")
        if head and tail and "(" not in func and ")" not in func:
            return func, head, tail
    return func, receiver, func


def _call_from_method_reference(
    items: list[CapturedItem], root: Any, source: bytes
) -> tuple[str | None, str] | None:
    del items
    if root.type != "method_reference":
        return None
    named = [child for child in root.children if child.is_named]
    if not named:
        return None
    func = slice_text(named[-1], source).strip()
    if not func:
        return None
    receiver = None
    if len(named) >= 2:
        receiver = generic_dotted(named[0], source).strip() or None
    return receiver, func


def _call_from_captures(
    items: list[CapturedItem], root: Any, source: bytes
) -> tuple[str | None, str] | None:
    del root
    obj_node = first_node(items, ".object", ".from")
    func_node = first_node(items, ".function", ".name", ".to")
    if func_node is None:
        return None
    receiver = None
    if obj_node is not None:
        receiver = generic_dotted(obj_node, source).strip() or None
    if func_node.type == "identifier":
        func = slice_text(func_node, source).strip()
    else:
        func = generic_dotted(func_node, source).strip()
    return receiver, func


def _call_from_root_fields(
    items: list[CapturedItem], root: Any, source: bytes
) -> tuple[str | None, str] | None:
    del items
    fn = root.child_by_field_name("function")
    if fn is None:
        fn = root.child_by_field_name("name")
    if fn is None:
        return None
    func = generic_dotted(fn, source).strip()
    recv_node = root.child_by_field_name("object")
    receiver = (
        generic_dotted(recv_node, source).strip() if recv_node is not None else None
    )
    return receiver or None, func


def _call_from_identifier(
    items: list[CapturedItem], root: Any, source: bytes
) -> tuple[str | None, str] | None:
    del items
    for child in root.children:
        if child.type == "identifier":
            return None, slice_text(child, source).strip()
    return None


_CALL_RULES: tuple[
    Callable[[list[CapturedItem], Any, bytes], tuple[str | None, str] | None], ...
] = (
    _call_from_method_reference,
    _call_from_captures,
    _call_from_root_fields,
    _call_from_identifier,
)


def extract_call_parts(
    items: list[CapturedItem], root: Any, source: bytes
) -> tuple[str, str | None, str]:
    for build in _CALL_RULES:
        hit = build(items, root, source)
        if hit is not None:
            receiver, func = hit
            if func:
                return _compose_call(receiver, func)
    return "", None, ""


def _is_dotted_name(text: str) -> bool:
    parts = (text or "").split(".")
    if not parts:
        return False
    return all(part.isidentifier() for part in parts)


def collect_simple_var_aliases(root: Any, source: bytes) -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "assignment":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is not None and right is not None:
                var = generic_dotted(left, source).strip()[:128]
                if var and _is_dotted_name(var):
                    target = _call_target_name(right, source)
                    if target:
                        found.append((var, target, node.start_byte))
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
                    found.append((var, target, node.start_byte))
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
