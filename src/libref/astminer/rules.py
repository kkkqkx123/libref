"""Language rule functions: capture selection, text shaping, alias building."""

from __future__ import annotations

from typing import Any

CALL_NODE_TYPES = frozenset(
    {
        "call",
        "call_expression",
        "method_invocation",
        "new_expression",
        "object_creation_expression",
        "method_reference",
        "explicit_constructor_invocation",
        "macro_invocation",
        "invocation_expression",
        "go_statement",
        "defer_statement",
    }
)

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
    }
)

BUILTIN_BARE_CALLS = frozenset(
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


def is_builtin_bare(raw: str, alias_map: dict[str, str]) -> bool:
    head = raw.partition(".")[0]
    return bool(head) and head in BUILTIN_BARE_CALLS and head not in alias_map


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


def slice_text(node: Any, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


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
    if pnode.type == "identifier":
        return slice_text(pnode, source).strip()
    named = pnode.child_by_field_name("name")
    if named is not None:
        return slice_text(named, source).strip()
    for child in pnode.children:
        if child.type == "identifier":
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
    return slice_text(node, source).strip()[:128]


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
            if name_node is not None and value_node is not None:
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
    return generic_dotted(fn, source).strip()[:128]


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


def build_aliases(
    language: str,
    root: Any,
    captures: dict[str, list],
    source: bytes,
    current_module: str | None = None,
    importer_file: str | None = None,
) -> list[tuple[str, str]]:
    if language == "python":
        return _python_aliases(root, captures, source, current_module)
    if language == "java":
        return _java_aliases(root, source)
    if language == "javascript":
        return _javascript_aliases(root, captures, source, importer_file)
    if language == "rust":
        return _rust_aliases(root, captures, source)
    if language == "go":
        return _go_aliases(root, captures, source)
    if language == "csharp":
        return _csharp_aliases(root, captures, source)
    return []


def _python_aliases(
    root: Any,
    captures: dict[str, list],
    source: bytes,
    current_module: str | None = None,
) -> list[tuple[str, str]]:
    if root.type == "import_statement":
        alias_node = first_node(captures, ".alias.name")
        if alias_node is not None:
            module = first_node(captures, ".alias.module")
            full = generic_dotted(module, source).strip() if module is not None else ""
            alias = slice_text(alias_node, source).strip()
            if full and alias:
                return [(alias, full)]
            return []
        found: list[tuple[str, str]] = []
        for name, nodes in captures.items():
            if not name.endswith(".module") or ".alias." in name:
                continue
            for module in nodes:
                full = generic_dotted(module, source).strip()
                if full:
                    found.append((full.split(".")[0], full))
        return found
    if root.type == "import_from_statement":
        module = first_node(captures, ".from.module")
        module_text = ""
        if module is not None:
            module_text = generic_dotted(module, source).strip().strip(".")
        else:
            relative = first_node(captures, ".relative")
            if relative is not None:
                from libref.astminer.context import resolve_python_relative

                resolved = resolve_python_relative(
                    current_module, slice_text(relative, source).strip()
                )
                if resolved is None:
                    return []
                module_text = resolved
        if module_text.startswith(".") or ". " in module_text:
            return []
        found = []
        seen_import = False
        for child in root.children:
            if child.type == "import":
                seen_import = True
                continue
            if not seen_import:
                continue
            if child.type in ("dotted_name", "identifier"):
                name = slice_text(child, source).strip()
                if not name:
                    continue
                full = f"{module_text}.{name}" if module_text else name
                found.append((name, full))
            elif child.type == "aliased_import":
                original, alias = _split_aliased(child, source)
                if original:
                    full = f"{module_text}.{original}" if module_text else original
                    found.append((alias or original, full))
        return found
    return []


def _module_path(node: Any, source: bytes) -> str:
    return slice_text(node, source).strip().strip("\"'")


def _javascript_aliases(
    root: Any,
    captures: dict[str, list],
    source: bytes,
    importer_file: str | None = None,
) -> list[tuple[str, str]]:
    def _normalized(raw_mod: str) -> str:
        from libref.astminer.context import normalize_js_specifier

        return normalize_js_specifier(raw_mod, importer_file)

    module = first_node(captures, ".import.module")
    if root.type == "import_statement" and module is not None:
        mod = _normalized(_module_path(module, source))
        found: list[tuple[str, str]] = []
        default = first_node(captures, ".default.name")
        if default is not None:
            name = slice_text(default, source).strip()
            if name:
                found.append((name, mod))
        for name, nodes in captures.items():
            if not name.endswith(".named.spec"):
                continue
            for spec in nodes:
                orig_node = spec.child_by_field_name("name")
                alias_node = spec.child_by_field_name("alias")
                original = (
                    slice_text(orig_node, source).strip()
                    if orig_node is not None
                    else ""
                )
                alias = (
                    slice_text(alias_node, source).strip()
                    if alias_node is not None
                    else ""
                )
                if original:
                    full = f"{mod}.{original}" if mod else original
                    found.append((alias or original, full))
        namespace = first_node(captures, ".namespace.name")
        if namespace is not None:
            name = slice_text(namespace, source).strip()
            if name:
                found.append((name, mod))
        return [(alias, full) for alias, full in found if alias and full]
    if root.type == "variable_declarator":
        callee = first_node(captures, ".require.callee")
        if callee is None or slice_text(callee, source).strip() != "require":
            return []
        module = first_node(captures, ".require.module")
        if module is None:
            return []
        mod = _normalized(_module_path(module, source))
        if not mod:
            return []
        name_node = first_node(captures, ".require.name")
        if name_node is not None:
            name = slice_text(name_node, source).strip()
            return [(name, mod)] if name else []
        destruct = first_node(captures, ".require.destruct")
        if destruct is None:
            return []
        found = []
        for child in destruct.children:
            if child.type in (
                "shorthand_property_identifier",
                "shorthand_property_identifier_pattern",
            ):
                prop = slice_text(child, source).strip()
                if prop:
                    found.append((prop, f"{mod}.{prop}"))
            elif child.type in ("pair", "pair_pattern"):
                key = child.child_by_field_name("key")
                value = child.child_by_field_name("value")
                if key is not None and value is not None:
                    alias = slice_text(value, source).strip()
                    original = slice_text(key, source).strip()
                    if alias and original:
                        found.append((alias, f"{mod}.{original}"))
        return found
    return []


def _java_aliases(root: Any, source: bytes) -> list[tuple[str, str]]:
    for child in root.children:
        if child.type == "asterisk":
            return []
    path_node = None
    for child in root.children:
        if child.type in ("scoped_identifier", "identifier"):
            path_node = child
    if path_node is None:
        return []
    full = slice_text(path_node, source).strip()
    if not full or "*" in full:
        return []
    alias = full.split(".")[-1]
    if not alias:
        return []
    return [(alias, full)]


_RUST_STDLIB_HEADS = frozenset({"std", "core", "alloc"})


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


def _rust_aliases(
    root: Any, captures: dict[str, list], source: bytes
) -> list[tuple[str, str]]:
    if root.type != "use_declaration":
        return []
    if first_node(captures, ".use.wildcard") is not None:
        return []
    alias_path = first_node(captures, ".use.alias.path")
    if alias_path is not None:
        alias_node = first_node(captures, ".use.alias.name")
        alias = slice_text(alias_node, source).strip() if alias_node is not None else ""
        full = generic_dotted(alias_path, source).strip()
        if not alias or not full or full.split(".")[0] in _RUST_STDLIB_HEADS:
            return []
        return [(alias, full)]
    list_path = first_node(captures, ".use.list.path")
    items = first_node(captures, ".use.list.items")
    if list_path is not None and items is not None:
        base = generic_dotted(list_path, source).strip()
        if not base or base.split(".")[0] in _RUST_STDLIB_HEADS:
            return []
        found: list[tuple[str, str]] = []
        for child in items.children:
            if child.type == "identifier":
                name = slice_text(child, source).strip()
                if name:
                    found.append((name, f"{base}.{name}"))
            elif child.type == "use_as_clause":
                path_child = child.child_by_field_name("path")
                alias_child = child.child_by_field_name("alias")
                if path_child is None or alias_child is None:
                    continue
                original = generic_dotted(path_child, source).strip()
                alias = slice_text(alias_child, source).strip()
                if original and alias:
                    found.append((alias, f"{base}.{original}"))
        return found
    path_node = first_node(captures, ".use.path")
    if path_node is None:
        return []
    full = generic_dotted(path_node, source).strip()
    if not full or full.split(".")[0] in _RUST_STDLIB_HEADS:
        return []
    alias = full.split(".")[-1]
    if not alias:
        return []
    return [(alias, full)]


def _go_aliases(
    root: Any, captures: dict[str, list], source: bytes
) -> list[tuple[str, str]]:
    if root.type != "import_spec":
        return []
    path_node = first_node(captures, ".import.alias.path", ".import.path")
    if path_node is None:
        return []
    raw_path = slice_text(path_node, source).strip().strip("\"'")
    if not raw_path:
        return []
    dotted = raw_path.replace("/", ".")
    head = raw_path.split("/")[0]
    if "." not in head:
        return []
    name_node = first_node(captures, ".import.alias.name")
    if name_node is not None:
        name = slice_text(name_node, source).strip()
        if (
            not name
            or name in (".", "_")
            or name_node.type in ("dot", "blank_identifier")
        ):
            return []
        return [(name, dotted)]
    segments = [seg for seg in raw_path.split("/") if seg]
    if not segments:
        return []
    local = segments[-1]
    if (
        len(segments) >= 2
        and len(local) >= 2
        and local[0] == "v"
        and local[1:].isdigit()
    ):
        local = segments[-2]
    if not local:
        return []
    return [(local, dotted)]


def _csharp_aliases(
    root: Any, captures: dict[str, list], source: bytes
) -> list[tuple[str, str]]:
    if root.type != "using_directive":
        return []
    alias_node = first_node(captures, ".using.alias.name")
    if alias_node is not None:
        alias = slice_text(alias_node, source).strip()
        path_node = first_node(captures, ".using.alias.path")
        if path_node is None:
            for child in root.children:
                if (
                    child is not alias_node
                    and child.is_named
                    and child.type in ("identifier", "qualified_name")
                ):
                    path_node = child
                    break
        full = (
            generic_dotted(path_node, source).strip() if path_node is not None else ""
        )
        if not alias or not full:
            return []
        return [(alias, full)]
    target = None
    for child in root.children:
        if child.is_named and child.type in ("identifier", "qualified_name"):
            target = child
            break
    if target is None:
        return []
    full = generic_dotted(target, source).strip()
    if not full:
        return []
    alias = full.split(".")[-1]
    if not alias:
        return []
    return [(alias, full)]
