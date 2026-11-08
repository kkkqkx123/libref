"""通用遍历引擎：类、函数、导入、调用的单遍抽取。"""

from __future__ import annotations

from tree_sitter import Parser

from libref.astminer.config import LanguageConfig, grammar_fingerprint, load_language
from libref.astminer.models import ApiDeclRecord, RawCall

_LITERAL_TYPES = frozenset(
    {
        "string",
        "string_fragment",
        "integer",
        "float",
        "true",
        "false",
        "none",
        "null",
        "number",
    }
)

_parsers: dict[str, tuple[str, Parser]] = {}


def get_parser(cfg: LanguageConfig) -> Parser:
    fp = grammar_fingerprint(cfg)
    cached = _parsers.get(cfg.name)
    if cached is not None and cached[0] == fp:
        return cached[1]
    parser = Parser(load_language(cfg))
    _parsers[cfg.name] = (fp, parser)
    return parser


def node_text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def dotted_text(node, source: bytes) -> str:
    obj = node.child_by_field_name("object")
    if obj is None:
        obj = node.child_by_field_name("value")
    name_node = node.child_by_field_name("name")
    if obj is not None and name_node is not None:
        return f"{dotted_text(obj, source)}.{node_text(name_node, source).strip()}"
    if node.type in ("dotted_name", "scoped_identifier"):
        names = [
            node_text(c, source).strip()
            for c in node.children
            if c.type == "identifier"
        ]
        names = [n for n in names if n]
        if names:
            return ".".join(names)
    if node.type == "identifier":
        return node_text(node, source).strip()
    return node_text(node, source).replace(" ", "").strip()


def summarize_arg(node, source: bytes) -> str:
    if node.type == "keyword_argument":
        name_node = node.child_by_field_name("name")
        value_node = node.child_by_field_name("value")
        name = node_text(name_node, source).strip() if name_node is not None else ""
        prefix = f"{name}=" if name else "kw="
        if value_node is None:
            return prefix
        value = summarize_arg(value_node, source)
        if value == "?":
            return prefix
        return f"{prefix}{value}"[:64]
    if node.type in _LITERAL_TYPES:
        text = node_text(node, source).strip().replace("\n", " ")
        return text[:48]
    if node.type == "identifier":
        return node_text(node, source).strip()[:48]
    if node.type in ("attribute", "dotted_name", "field_access", "scoped_identifier"):
        return dotted_text(node, source)[:48]
    if node.type in ("list", "tuple", "dictionary", "set", "list_pattern"):
        return "[...]"
    if node.type in ("list_splat", "dictionary_splat"):
        return node_text(node, source).strip().replace("\n", " ")[:48]
    if node.type == "call":
        fn = node.child_by_field_name("function")
        if fn is not None:
            return f"{dotted_text(fn, source)[:32]}(...)"
        return "(...)"
    return "?"


def parse_argument_list(call_node, source: bytes) -> list[str]:
    args_node = None
    for child in call_node.children:
        if child.type == "argument_list":
            args_node = child
            break
    if args_node is None:
        args_node = call_node.child_by_field_name("arguments")
    if args_node is None:
        return []
    out: list[str] = []
    for child in args_node.children:
        if child.type in ("(", ")", ","):
            continue
        out.append(summarize_arg(child, source))
    return out


def parse_import_aliases(node, source: bytes) -> list[tuple[str, str]]:
    if node.type == "import_statement":
        return _parse_python_import(node, source)
    if node.type == "import_from_statement":
        return _parse_python_import_from(node, source)
    if node.type == "import_declaration":
        return _parse_java_import(node, source)
    return []


def _parse_python_import(node, source: bytes) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for child in node.children:
        if child.type == "dotted_name":
            full = node_text(child, source).strip()
            if full:
                found.append((full.split(".")[0], full))
        elif child.type == "aliased_import":
            original = ""
            alias = ""
            for sub in child.children:
                if sub.type == "dotted_name" and not original:
                    original = node_text(sub, source).strip()
                elif sub.type == "identifier" and original:
                    alias = node_text(sub, source).strip()
            if original:
                found.append((alias or original.split(".")[0], original))
    return found


def _parse_python_import_from(node, source: bytes) -> list[tuple[str, str]]:
    for child in node.children:
        if child.type == "relative_import":
            return []
    mod_node = node.child_by_field_name("module_name")
    module = node_text(mod_node, source).strip().strip(".") if mod_node is not None else ""
    if module.startswith(".") or ". " in module:
        return []
    found: list[tuple[str, str]] = []
    after_import = False
    for child in node.children:
        if child.type == "import":
            after_import = True
            continue
        if not after_import:
            continue
        if child.type in ("dotted_name", "identifier"):
            name = node_text(child, source).strip()
            if not name:
                continue
            full = f"{module}.{name}" if module else name
            found.append((name, full))
        elif child.type == "aliased_import":
            original = ""
            alias = ""
            for sub in child.children:
                if sub.type in ("dotted_name", "identifier") and not original:
                    original = node_text(sub, source).strip()
                elif sub.type == "identifier" and original:
                    alias = node_text(sub, source).strip()
            if original:
                full = f"{module}.{original}" if module else original
                found.append((alias or original, full))
    return found


def _parse_java_import(node, source: bytes) -> list[tuple[str, str]]:
    has_static = any(c.type == "static" for c in node.children)
    has_wildcard = any(c.type == "asterisk" for c in node.children)
    if has_wildcard:
        return []
    path_node = None
    for child in node.children:
        if child.type in ("scoped_identifier", "identifier"):
            path_node = child
    if path_node is None:
        return []
    full = node_text(path_node, source).strip()
    if not full or "*" in full:
        return []
    _ = has_static
    alias = full.split(".")[-1]
    if not alias:
        return []
    return [(alias, full)]


def extract_file(
    path: str, cfg: LanguageConfig, source: bytes
) -> tuple[list[ApiDeclRecord], list[tuple[str, str]], list[RawCall]]:
    parser = get_parser(cfg)
    tree = parser.parse(source)
    decls: list[ApiDeclRecord] = []
    aliases: list[tuple[str, str]] = []
    calls: list[RawCall] = []
    _walk(
        tree.root_node, cfg, source, path, [],
        None, decls, aliases, calls,
    )
    return decls, aliases, calls


def _decl_kind_for_class(node_type: str) -> str:
    if "interface" in node_type:
        return "interface"
    if "enum" in node_type:
        return "enum"
    return "class"


def _walk(
    node,
    cfg: LanguageConfig,
    source: bytes,
    path: str,
    stack: list[str],
    caller: str | None,
    decls: list[ApiDeclRecord],
    aliases: list[tuple[str, str]],
    calls: list[RawCall],
) -> None:
    if node.type in cfg.class_types:
        name = _decl_name(node, source)
        qual = ".".join([*stack, name]) if name else ".".join(stack)
        if name:
            decls.append(
                ApiDeclRecord(
                    qualified_name=qual or name,
                    kind=_decl_kind_for_class(node.type),
                    file=path,
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                )
            )
        stack = [*stack, name] if name else stack
        for child in node.children:
            _walk(child, cfg, source, path, stack, qual or caller,
                  decls, aliases, calls)
        return
    if node.type in cfg.function_types:
        name = _decl_name(node, source)
        qual = ".".join([*stack, name]) if name else (".".join(stack) or None)
        if "constructor" in node.type:
            kind = "constructor"
        else:
            kind = "method" if stack else "function"
        if name:
            decls.append(
                ApiDeclRecord(
                    qualified_name=qual or name,
                    kind=kind,
                    file=path,
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                )
            )
        next_caller = qual or caller
        next_stack = [*stack, name] if name else stack
        for child in node.children:
            _walk(child, cfg, source, path, next_stack, next_caller,
                  decls, aliases, calls)
        return
    if node.type in cfg.import_types:
        for alias, full in parse_import_aliases(node, source):
            if alias and full:
                aliases.append((alias, full))
        for child in node.children:
            _walk(child, cfg, source, path, stack, caller,
                  decls, aliases, calls)
        return
    if node.type in cfg.call_types:
        raw = _call_raw_text(node, cfg, source)
        if raw:
            calls.append(
                RawCall(
                    raw_callee=raw,
                    args=parse_argument_list(node, source),
                    caller_file=path,
                    caller=caller,
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                )
            )
        for child in node.children:
            _walk(child, cfg, source, path, stack, caller,
                  decls, aliases, calls)
        return
    if cfg.extra_walk_fn is not None:
        extra = cfg.extra_walk_fn(node, source, path, stack, caller)
        if extra is not None:
            return
    for child in node.children:
        _walk(child, cfg, source, path, stack, caller, decls, aliases, calls)


def _call_raw_text(node, cfg: LanguageConfig, source: bytes) -> str:
    fn = node.child_by_field_name(cfg.call_function_field)
    receiver = node.child_by_field_name(cfg.call_receiver_field)
    if fn is not None and receiver is not None:
        obj_text = node_text(receiver, source).strip()
        name_text = node_text(fn, source).strip()
        if obj_text and name_text:
            return f"{obj_text}.{name_text}"
        return obj_text or name_text
    if fn is not None:
        if fn.type in cfg.accessor_node_types:
            return dotted_text(fn, source)
        return node_text(fn, source).strip()
    for child in node.children:
        if child.type == "identifier":
            return node_text(child, source).strip()
    return ""


def _decl_name(node, source: bytes) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return node_text(name_node, source).strip()
    for child in node.children:
        if child.type == "identifier":
            return node_text(child, source).strip()
    return ""
