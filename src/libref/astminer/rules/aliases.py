"""Import alias construction per language."""

from __future__ import annotations

from typing import Any

from libref.astminer.rules.entities import _split_aliased, first_node
from libref.astminer.rules.text import generic_dotted, slice_text

_RUST_STDLIB_HEADS = frozenset({"std", "core", "alloc"})


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
        return _rust_aliases(root, captures, source, current_module)
    if language == "go":
        return _go_aliases(root, captures, source)
    if language == "csharp":
        return _csharp_aliases(root, captures, source)
    if language == "kotlin":
        return _kotlin_aliases(root, source)
    if language == "groovy":
        return _groovy_aliases(root, source)
    return []


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


def _groovy_aliases(root: Any, source: bytes) -> list[tuple[str, str]]:
    return _qualified_aliases(root, source)


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


def _javascript_aliases(
    root: Any,
    captures: dict[str, list],
    source: bytes,
    importer_file: str | None = None,
) -> list[tuple[str, str]]:
    def _normalized(raw_mod: str) -> str:
        from libref.astminer.context import normalize_js_specifier

        return normalize_js_specifier(raw_mod, importer_file)

    dyn_name = first_node(captures, ".import.dynamic.name")
    dyn_module = first_node(captures, ".import.dynamic.module")
    if root.type == "variable_declarator" and dyn_name is not None:
        mod = (
            _normalized(_module_path(dyn_module, source))
            if dyn_module is not None
            else ""
        )
        name = slice_text(dyn_name, source).strip()
        if name and mod:
            return [(name, mod)]
        return []
    if root.type == "export_statement":
        module = first_node(captures, ".export.module")
        if module is None:
            return []
        mod = _normalized(_module_path(module, source))
        if not mod:
            return []
        reexported: list[tuple[str, str]] = []
        for name, nodes in captures.items():
            if not name.endswith(".export.spec"):
                continue
            for spec in nodes:
                idents = [
                    slice_text(child, source).strip()
                    for child in spec.children
                    if child.type == "identifier"
                ]
                idents = [text for text in idents if text]
                if not idents:
                    continue
                original = idents[0]
                alias = idents[1] if len(idents) > 1 else original
                reexported.append((alias, f"{mod}.{original}"))
        return [(alias, full) for alias, full in reexported if alias and full]
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


def _kotlin_aliases(root: Any, source: bytes) -> list[tuple[str, str]]:
    return _qualified_aliases(root, source)


def _module_path(node: Any, source: bytes) -> str:
    return slice_text(node, source).strip().strip("\"'")


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


def _qualified_aliases(root: Any, source: bytes) -> list[tuple[str, str]]:
    for child in root.children:
        if child.type in ("asterisk", "wildcard_import", "*"):
            return []
    path_node = None
    alias_node = None
    for child in root.children:
        if (
            child.type in ("qualified_identifier", "qualified_name")
            and path_node is None
        ):
            path_node = child
        elif child.type == "identifier" and path_node is not None:
            alias_node = child
    if path_node is None:
        return []
    full = generic_dotted(path_node, source).strip()
    if not full:
        return []
    if alias_node is not None:
        alias = slice_text(alias_node, source).strip()
        if alias:
            return [(alias, full)]
    alias = full.split(".")[-1]
    if not alias:
        return []
    return [(alias, full)]


def _rust_aliases(
    root: Any,
    captures: dict[str, list],
    source: bytes,
    current_module: str | None = None,
) -> list[tuple[str, str]]:
    if root.type != "use_declaration":
        return []
    if first_node(captures, ".use.wildcard") is not None:
        return []
    alias_path = first_node(captures, ".use.alias.path")
    if alias_path is not None:
        alias_node = first_node(captures, ".use.alias.name")
        alias = slice_text(alias_node, source).strip() if alias_node is not None else ""
        full = resolve_rust_use_path(
            _rust_path_segments(alias_path, source), current_module
        )
        if not alias or not full or full.split(".")[0] in _RUST_STDLIB_HEADS:
            return []
        return [(alias, full)]
    list_path = first_node(captures, ".use.list.path")
    items = first_node(captures, ".use.list.items")
    if list_path is not None and items is not None:
        base = resolve_rust_use_path(
            _rust_path_segments(list_path, source), current_module
        )
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
                original = resolve_rust_use_path(
                    _rust_path_segments(path_child, source), current_module
                )
                alias = slice_text(alias_child, source).strip()
                if original and alias:
                    found.append((alias, f"{base}.{original}"))
        return found
    path_node = first_node(captures, ".use.path")
    if path_node is None:
        return []
    full = resolve_rust_use_path(_rust_path_segments(path_node, source), current_module)
    if not full or full.split(".")[0] in _RUST_STDLIB_HEADS:
        return []
    alias = full.split(".")[-1]
    if not alias:
        return []
    return [(alias, full)]


def _rust_path_segments(node: Any, source: bytes) -> list[str]:
    if node.type == "scoped_identifier":
        out: list[str] = []
        for child in node.children:
            if slice_text(child, source) == "::":
                continue
            out.extend(_rust_path_segments(child, source))
        return out
    text = slice_text(node, source).strip()
    return [text] if text else []


def normalize_go_import(dotted: str, module_prefix: str) -> str:
    prefix = (module_prefix or "").strip().strip(".")
    if not prefix or dotted == prefix:
        return dotted
    if dotted.startswith(prefix + "."):
        return dotted[len(prefix) + 1 :]
    return dotted


def resolve_rust_use_path(segments: list[str], current_module: str | None) -> str:
    parts = [seg for seg in segments if seg]
    if not parts:
        return ""
    head = parts[0]
    base = current_module.split(".") if current_module else []
    if head == "crate":
        rest = parts[1:]
    elif head in ("self", "Self"):
        rest = base + parts[1:]
    elif head == "super":
        depth = 0
        while depth < len(parts) and parts[depth] == "super":
            depth += 1
        rest = base[: max(0, len(base) - depth)] + parts[depth:]
    else:
        rest = parts
    return ".".join([seg for seg in rest if seg])
