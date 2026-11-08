"""通用抽取引擎：查询匹配、去重归并、记录装配。"""

from __future__ import annotations

from typing import Any

from tree_sitter import Parser, Query, QueryCursor

from libref.astminer import queries as _queries
from libref.astminer import rules as _rules
from libref.astminer.config import LanguageConfig, grammar_fingerprint, load_language
from libref.astminer.context import determine_module_path
from libref.astminer.models import ApiDeclRecord, RawCall

_parsers: dict[str, tuple[str, Parser]] = {}

_compiled: dict[str, dict[str, Query]] = {}

_CLASS_LIKE = frozenset({"class", "interface", "enum", "record", "annotation"})


def get_parser(cfg: LanguageConfig) -> Parser:
    fp = grammar_fingerprint(cfg)
    cached = _parsers.get(cfg.name)
    if cached is not None and cached[0] == fp:
        return cached[1]
    parser = Parser(load_language(cfg))
    _parsers[cfg.name] = (fp, parser)
    return parser


def _compiled_queries(cfg: LanguageConfig) -> dict[str, Query]:
    fp = grammar_fingerprint(cfg)
    key = f"{cfg.name}\x00{fp}"
    hit = _compiled.get(key)
    if hit is not None:
        return hit
    language = load_language(cfg)
    compiled = {
        kind: Query(language, _queries.get(cfg.name, kind))
        for kind in ("entity", "call", "dependency")
    }
    _compiled[key] = compiled
    return compiled


def node_text(node, source: bytes) -> str:
    return _rules.slice_text(node, source)


def _largest(captures: dict[str, list]) -> tuple[str, Any] | None:
    best: tuple[str, Any] | None = None
    best_size = -1
    for name, nodes in captures.items():
        for node in nodes:
            size = node.end_byte - node.start_byte
            if size > best_size:
                best_size = size
                best = (name, node)
    return best


def _merge_matches(matches: list) -> list[tuple[Any, dict[str, list]]]:
    merged: dict[tuple[int, int], tuple[Any, dict[str, list]]] = {}
    for _, captures in matches:
        pick = _largest(captures)
        if pick is None:
            continue
        _, node = pick
        key = (node.start_byte, node.end_byte)
        slot = merged.get(key)
        if slot is None:
            merged[key] = (node, {name: list(nodes) for name, nodes in captures.items()})
        else:
            _, existing = slot
            for name, nodes in captures.items():
                existing.setdefault(name, []).extend(nodes)
    return list(merged.values())


def _entity_name(captures: dict[str, list], roots: set[str], source: bytes) -> str:
    for root in roots:
        for node in captures.get(f"{root}.name", []):
            text = _rules.slice_text(node, source).strip()
            if text:
                return text
    for name, nodes in captures.items():
        if name.endswith(".signature.name") and nodes:
            text = _rules.slice_text(nodes[0], source).strip()
            if text:
                return text
    for suffix in (".name", ".func"):
        for name, nodes in captures.items():
            if name.endswith(suffix) and nodes:
                text = _rules.slice_text(nodes[0], source).strip()
                if text:
                    return text
    return ""


def _decl_bases(
    captures: dict[str, list], source: bytes
) -> list[str]:
    bases: list[str] = []
    for text in _rules.all_texts(captures, source, ".signature.base", ".base"):
        if text not in bases:
            bases.append(text)
    return bases


def extract_file(
    path: str, cfg: LanguageConfig, source: bytes
) -> tuple[list[ApiDeclRecord], list[tuple[str, str]], list[RawCall]]:
    parser = get_parser(cfg)
    tree = parser.parse(source)
    compiled = _compiled_queries(cfg)
    entity_matches = QueryCursor(compiled["entity"]).matches(tree.root_node)
    call_matches = QueryCursor(compiled["call"]).matches(tree.root_node)
    dep_matches = QueryCursor(compiled["dependency"]).matches(tree.root_node)
    module = determine_module_path(path, cfg.name)
    decls = _build_decls(path, cfg, source, entity_matches, dep_matches, module)
    alias_module = module
    if module is not None and path.replace("\\", "/").endswith("__init__.py"):
        alias_module = f"{module}.__init__"
    aliases = _build_aliases(cfg, source, dep_matches, alias_module)
    import_map = dict(aliases)
    for var, target in _rules.collect_simple_var_aliases(tree.root_node, source):
        if not var or not target or var in import_map:
            continue
        head, _, rest = target.partition(".")
        if rest and head in import_map:
            expanded = import_map[head] + "." + rest
        elif not rest and head in import_map:
            expanded = import_map[head]
        else:
            expanded = target
        if var and expanded:
            aliases.append((var, expanded))
            import_map[var] = expanded
    calls = _build_calls(path, cfg, source, call_matches, decls)
    return decls, aliases, calls


def _build_decls(path, cfg, source, entity_matches, dep_matches, module=None) -> list[ApiDeclRecord]:
    groups = _merge_matches(entity_matches)
    cands: list[dict] = []
    for node, captures in groups:
        roots = {
            name
            for name in captures
            if name.startswith("entity.") and name.count(".") == 1
        }
        kinds = {
            kind
            for kind in (_rules.entity_kind({root}) for root in roots)
            if kind is not None
        }
        kind = _rules.refine_kind(kinds)
        if kind is None:
            continue
        name = _entity_name(captures, roots, source)
        if not name:
            continue
        cands.append(
            {"node": node, "kind": kind, "name": name, "captures": captures}
        )
    cands.sort(key=lambda c: (c["node"].start_byte, -c["node"].end_byte))
    dep_spans: list[tuple[int, int, str]] = []
    for node, captures in _merge_matches(dep_matches):
        for suffix in (".extend.name", ".implement.name"):
            for name, nodes in captures.items():
                if name.endswith(suffix):
                    for dep_node in nodes:
                        text = _rules.generic_dotted(dep_node, source).strip()
                        if text:
                            dep_spans.append(
                                (dep_node.start_byte, dep_node.end_byte, text)
                            )
    dep_spans.sort()
    decls: list[ApiDeclRecord] = []
    stack: list[ApiDeclRecord] = []
    for cand in cands:
        node = cand["node"]
        while stack and not (
            stack[-1].start_byte <= node.start_byte
            and node.end_byte <= stack[-1].end_byte
        ):
            stack.pop()
        parent = stack[-1].qualified_name if stack else None
        depth = len(stack)
        kind = cand["kind"]
        if kind == "function" and stack and stack[-1].kind in _CLASS_LIKE:
            kind = "method"
        if parent:
            qual = f"{parent}.{cand['name']}"
        elif module and (module == cand["name"] or module.endswith("." + cand["name"])):
            qual = module
        elif module:
            qual = f"{module}.{cand['name']}"
        else:
            qual = cand["name"]
        captures = cand["captures"]
        body = _rules.first_node(captures, ".body")
        if body is not None:
            signature = source[node.start_byte : body.start_byte].decode(
                "utf-8", "replace"
            ).strip()
        else:
            signature = _rules.slice_text(node, source).strip()
        params_node = _rules.first_node(captures, ".params", ".signature.params")
        params = (
            _rules.params_from_params_node(params_node, source)
            if params_node is not None
            else []
        )
        param_types = (
            _rules.param_types_from_params_node(params_node, source)
            if params_node is not None
            else []
        )
        return_node = _rules.first_node(captures, ".return_type", ".signature.return_type")
        return_type = _rules.shaped_return(return_node, source)
        bases = _decl_bases(captures, source)
        for start, end, text in dep_spans:
            if node.start_byte <= start and end <= node.end_byte and text not in bases:
                bases.append(text)
        decls.append(
            ApiDeclRecord(
                qualified_name=qual,
                kind=kind,
                file=path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                signature=signature,
                params=params,
                param_types=param_types,
                return_type=return_type,
                bases=bases,
                parent=parent,
                depth=depth,
            )
        )
        stack.append(decls[-1])
    return decls


def _build_aliases(cfg, source, dep_matches, module: str | None = None) -> list[tuple[str, str]]:
    aliases: list[tuple[str, str]] = []
    for node, captures in _merge_matches(dep_matches):
        for alias, full in _rules.build_aliases(cfg.name, node, captures, source, module):
            if alias and full:
                aliases.append((alias, full))
    return aliases


def _call_texts(
    root, captures: dict[str, list], source: bytes
) -> tuple[str, str | None, str]:
    obj_node = _rules.first_node(captures, ".object", ".from")
    func_node = _rules.first_node(captures, ".function", ".name", ".to")
    receiver = None
    if obj_node is not None:
        receiver = _rules.generic_dotted(obj_node, source).strip() or None
    func = ""
    if func_node is not None:
        if func_node.type == "identifier":
            func = _rules.slice_text(func_node, source).strip()
        else:
            func = _rules.generic_dotted(func_node, source).strip()
    if not func:
        fn = root.child_by_field_name("function")
        if fn is None:
            fn = root.child_by_field_name("name")
        if fn is not None:
            func = _rules.generic_dotted(fn, source).strip()
            recv_node = root.child_by_field_name("object")
            if recv_node is not None and receiver is None:
                receiver = _rules.generic_dotted(recv_node, source).strip() or None
    if not func:
        for child in root.children:
            if child.type == "identifier":
                func = _rules.slice_text(child, source).strip()
                break
    if receiver and func:
        return f"{receiver}.{func}", receiver, func
    if func and receiver is not None and "." in func:
        head, _, _ = func.rpartition(".")
        return func, head or None, func
    return func, receiver, func


def _build_calls(path, cfg, source, call_matches, decls) -> list[RawCall]:
    groups = _merge_matches(call_matches)
    calls: list[RawCall] = []
    for node, captures in groups:
        if node.type not in _rules.CALL_NODE_TYPES:
            continue
        if _is_module_loader(node, source):
            continue
        raw, receiver, func = _call_texts(node, captures, source)
        if not raw:
            continue
        args_node = _rules.first_node(captures, ".arguments")
        if args_node is None:
            args_node = node.child_by_field_name("arguments")
        if args_node is None:
            for child in node.children:
                if child.type == "argument_list":
                    args_node = child
                    break
        args = (
            _rules.summarize_arguments(args_node, source)
            if args_node is not None
            else []
        )
        count = _rules.count_arguments(args_node) if args_node is not None else 0
        caller = _caller_for(node, decls)
        calls.append(
            RawCall(
                raw_callee=raw,
                args=args,
                caller_file=path,
                caller=caller,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                receiver=receiver,
                func=func,
                argument_count=count,
            )
        )
    return calls


def _is_module_loader(node, source: bytes) -> bool:
    if node.type != "call_expression":
        return False
    fn = node.child_by_field_name("function")
    if fn is None:
        return False
    if fn.type == "import":
        return True
    return fn.type == "identifier" and _rules.slice_text(fn, source).strip() == "require"


def _caller_for(node, decls: list[ApiDeclRecord]) -> str | None:
    best: ApiDeclRecord | None = None
    for decl in decls:
        if not (decl.start_byte <= node.start_byte and node.end_byte <= decl.end_byte):
            continue
        if best is None or (
            decl.start_byte >= best.start_byte and decl.end_byte <= best.end_byte
        ):
            best = decl
    return best.qualified_name if best is not None else None
