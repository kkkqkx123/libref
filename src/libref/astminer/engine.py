"""通用遍历引擎：类、函数、导入、调用的单遍抽取。"""

from __future__ import annotations

from tree_sitter import Parser

from libref.astminer.config import LanguageConfig, load_language
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

_parsers: dict[str, Parser] = {}


def get_parser(cfg: LanguageConfig) -> Parser:
    parser = _parsers.get(cfg.name)
    if parser is None:
        parser = Parser(load_language(cfg))
        _parsers[cfg.name] = parser
    return parser


def node_text(node, source: bytes) -> str:
    try:
        return source[node.start_byte : node.end_byte].decode("utf-8", "replace")
    except Exception:
        return ""


def dotted_text(node, source: bytes) -> str:
    parts: list[str] = []
    cur = node
    while True:
        t = node_text(cur, source)
        if cur.type == "identifier":
            parts.append(t.strip())
            break
        if cur.type in ("dotted_name", "attribute", "field_access", "scoped_identifier"):
            left = None
            right = None
            for child in cur.children:
                if child.type in ("identifier", "dotted_name", "attribute",
                                  "field_access", "scoped_identifier"):
                    if left is None and right is None:
                        left = child
                    else:
                        right = child
            if left is not None and right is not None:
                parts.append(dotted_text(right, source))
                cur = left
                continue
            cleaned = t.replace(" ", "")
            if cleaned:
                parts.append(cleaned)
            break
        parts.append(t.strip())
        break
    parts = [p for p in parts if p]
    return ".".join(reversed(parts))


def summarize_arg(node, source: bytes) -> str:
    if node.type == "keyword_argument":
        name_node = node.child_by_field_name("name")
        name = node_text(name_node, source).strip() if name_node is not None else ""
        return f"{name}=" if name else "kw="
    if node.type in _LITERAL_TYPES:
        text = node_text(node, source).strip().replace("\n", " ")
        return text[:48]
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
    found: list[tuple[str, str]] = []
    if node.type == "import_statement":
        target = node.child_by_field_name("name")
        if target is None:
            return []
        if target.type == "aliased_import":
            original = ""
            alias = ""
            for child in target.children:
                if child.type in ("dotted_name",):
                    original = node_text(child, source).strip()
                elif child.type == "identifier" and original:
                    alias = node_text(child, source).strip()
            if original:
                found.append((alias or original.split(".")[0], original))
        elif target.type == "dotted_name":
            full = node_text(target, source).strip()
            found.append((full.split(".")[0], full))
    elif node.type == "import_from_statement":
        mod_node = node.child_by_field_name("module_name")
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return []
        module = ""
        if mod_node is not None:
            module = node_text(mod_node, source).strip().strip(".")
        if name_node.type == "aliased_import":
            original = ""
            alias = ""
            for child in name_node.children:
                if child.type in ("dotted_name", "identifier") and not original:
                    original = node_text(child, source).strip()
                elif child.type == "identifier" and original:
                    alias = node_text(child, source).strip()
            if original:
                full = f"{module}.{original}" if module else original
                found.append((alias or original, full))
        elif name_node.type in ("dotted_name", "identifier"):
            for piece in _split_import_names(name_node, source):
                full = f"{module}.{piece[0]}" if module else piece[0]
                found.append((piece[1] or piece[0], full))
    elif node.type == "import_declaration":
        text = node_text(node, source)
        body = text.strip()
        if body.startswith("import "):
            body = body[len("import ") :].strip().rstrip(";").strip()
            for chunk in body.split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                found.append((chunk.split(".")[-1], chunk))
    return found


def _split_import_names(name_node, source: bytes) -> list[tuple[str, str]]:
    text = node_text(name_node, source).strip()
    out: list[tuple[str, str]] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        out.append((chunk, ""))
    return out


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
        if name and node.start_point and node.end_point:
            decls.append(
                ApiDeclRecord(
                    qualified_name=qual or name,
                    kind="class",
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
        fn = node.child_by_field_name(cfg.call_function_field)
        raw = ""
        if fn is not None:
            if fn.type in cfg.accessor_node_types or fn.type in (
                "attribute", "field_access", "dotted_name", "scoped_identifier",
            ):
                raw = dotted_text(fn, source)
            else:
                raw = node_text(fn, source).strip()
        else:
            for child in node.children:
                if child.type == "identifier":
                    raw = node_text(child, source).strip()
                    break
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


def _decl_name(node, source: bytes) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return node_text(name_node, source).strip()
    for child in node.children:
        if child.type == "identifier":
            return node_text(child, source).strip()
    return ""
