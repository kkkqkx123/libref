"""Generic extraction engine: query matching, deduplication/merging, record assembly."""

from __future__ import annotations

import bisect
from typing import Any

from tree_sitter import Parser, Query, QueryCursor

from libref.astminer import queries as _queries
from libref.astminer import rules as _rules
from libref.astminer.config import LanguageConfig, grammar_fingerprint, load_language
from libref.astminer.context import determine_module_path
from libref.astminer.models import ApiDeclRecord, RawCall
from libref.astminer.rules import normalize_owner_text

_parsers: dict[str, tuple[str, Parser]] = {}

_compiled: dict[str, dict[str, Query]] = {}

_CLASS_LIKE = frozenset(
    {"class", "interface", "enum", "record", "annotation", "struct", "union"}
)


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
        for kind in ("entity", "call", "dependency", "comment")
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
            merged[key] = (
                node,
                {name: list(nodes) for name, nodes in captures.items()},
            )
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


def _decl_bases(captures: dict[str, list], source: bytes) -> list[str]:
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
    comment_matches = QueryCursor(compiled["comment"]).matches(tree.root_node)
    module = determine_module_path(path, cfg.name)
    doc_nodes, line_nodes = _comment_spans(comment_matches)
    decls = _build_decls(
        path,
        cfg,
        source,
        entity_matches,
        dep_matches,
        module,
        doc_nodes=doc_nodes,
        line_nodes=line_nodes,
    )
    alias_module = module
    if module is not None and path.replace("\\", "/").endswith("__init__.py"):
        alias_module = f"{module}.__init__"
    aliases = _build_aliases(cfg, source, dep_matches, alias_module, path)
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


def _comment_spans(
    comment_matches,
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    doc: list[tuple[int, int]] = []
    line: list[tuple[int, int]] = []
    for _, captures in comment_matches:
        for name, nodes in captures.items():
            for node in nodes:
                span = (node.start_byte, node.end_byte)
                if name.endswith((".docstring", ".doc")):
                    doc.append(span)
                elif name.endswith(".line"):
                    line.append(span)
    doc.sort()
    line.sort()
    return doc, line


def _body_opens_with_string(body, source: bytes) -> bool:
    for child in body.children:
        if not child.is_named:
            continue
        if child.type != "expression_statement":
            return False
        for sub in child.children:
            if sub.is_named:
                return sub.type == "string"
        return False
    return False


def _has_doc(
    node,
    body,
    source: bytes,
    doc_nodes: list[tuple[int, int]],
    line_nodes: list[tuple[int, int]],
) -> bool:
    if body is not None and _body_opens_with_string(body, source):
        return True
    start = node.start_byte
    head_end = body.start_byte if body is not None else node.end_byte
    for span_start, span_end in doc_nodes:
        if start <= span_start and span_end <= head_end:
            return True
    for span_start, span_end in line_nodes:
        if start <= span_start and span_end <= head_end:
            return True
    spans = sorted(doc_nodes + line_nodes, key=lambda span: span[1])
    ends = [end for _, end in spans]
    idx = bisect.bisect_right(ends, start) - 1
    if idx >= 0:
        _, end = spans[idx]
        if end <= start and source[end:start].strip() == b"":
            return True
    return False


def _build_decls(
    path,
    cfg,
    source,
    entity_matches,
    dep_matches,
    module=None,
    doc_nodes=None,
    line_nodes=None,
) -> list[ApiDeclRecord]:
    doc_spans = list(doc_nodes or [])
    line_spans = list(line_nodes or [])
    groups = _merge_matches(entity_matches)
    cands: list[dict] = []
    impl_anchors: list[tuple[int, int, str]] = []
    for node, captures in groups:
        roots = {
            name
            for name in captures
            if name.startswith("entity.") and name.count(".") == 1
        }
        if "entity.impl" in roots:
            owner_node = _rules.first_node(captures, ".type.name")
            owner = (
                normalize_owner_text(_rules.slice_text(owner_node, source))
                if owner_node is not None
                else ""
            )
            if owner:
                impl_anchors.append((node.start_byte, node.end_byte, owner))
            continue
        kinds = {
            kind
            for kind in (_rules.entity_kind({root}) for root in roots)
            if kind is not None
        }
        kind = _rules.refine_kind(kinds)
        if kind is None:
            continue
        if kind == "class" and any(
            child.type == "interface" for child in node.children
        ):
            kind = "interface"
        name = _entity_name(captures, roots, source)
        if not name:
            continue
        owner_node = _rules.first_node(captures, ".owner", ".receiver")
        owner = (
            normalize_owner_text(_rules.slice_text(owner_node, source))
            if owner_node is not None
            else ""
        )
        cands.append(
            {
                "node": node,
                "kind": kind,
                "name": name,
                "captures": captures,
                "owner": owner,
                "file_scoped": any(
                    capture == "entity.package.file_scoped" for capture in captures
                ),
            }
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
    decl_nodes: list = []
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
        if kind == "package" and parent is None:
            qual = cand["name"]
        elif parent:
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
            signature = (
                source[node.start_byte : body.start_byte]
                .decode("utf-8", "replace")
                .strip()
            )
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
        return_node = _rules.first_node(
            captures, ".return_type", ".signature.return_type"
        )
        return_type = _rules.shaped_return(return_node, source)
        bases = _decl_bases(captures, source)
        decl_nodes.append(node)
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
                has_doc=_has_doc(node, body, source, doc_spans, line_spans),
            )
        )
        stack.append(decls[-1])
    _attach_bases_innermost(decls, decl_nodes, dep_spans)
    _claim_file_scoped(decls, cands, cfg.name)
    _reparent_by_owner(decls, cands, impl_anchors, module)
    _merge_partial_decls(decls)
    return decls


def _attach_bases_innermost(decls, decl_nodes, dep_spans) -> None:
    for start, end, text in dep_spans:
        best: ApiDeclRecord | None = None
        best_size: int | None = None
        for decl, dnode in zip(decls, decl_nodes):
            if not (dnode.start_byte <= start and end <= dnode.end_byte):
                continue
            size = dnode.end_byte - dnode.start_byte
            if best is None or (best_size is not None and size < best_size):
                best, best_size = decl, size
        if best is not None and text not in best.bases:
            best.bases.append(text)


_PACKAGE_CLAIM_LANGUAGES = frozenset({"java", "csharp", "kotlin", "groovy"})

_PARTIAL_MERGE_KINDS = frozenset({"class", "interface", "struct", "record", "enum"})


def _merge_partial_decls(decls: list[ApiDeclRecord]) -> None:
    first_at: dict[tuple[str | None, str], int] = {}
    drop: set[int] = set()
    for index, decl in enumerate(decls):
        if decl.kind not in _PARTIAL_MERGE_KINDS:
            continue
        key = (decl.parent, decl.qualified_name)
        prev = first_at.get(key)
        if prev is None:
            first_at[key] = index
            continue
        anchor = decls[prev]
        anchor.start_byte = min(anchor.start_byte, decl.start_byte)
        anchor.end_byte = max(anchor.end_byte, decl.end_byte)
        anchor.line_start = min(anchor.line_start, decl.line_start)
        anchor.line_end = max(anchor.line_end, decl.line_end)
        for base in decl.bases:
            if base not in anchor.bases:
                anchor.bases.append(base)
        anchor.has_doc = anchor.has_doc or decl.has_doc
        if not anchor.signature and decl.signature:
            anchor.signature = decl.signature
        drop.add(index)
    for index in sorted(drop, reverse=True):
        del decls[index]


def _claim_file_scoped(
    decls: list[ApiDeclRecord], cands: list[dict], language: str | None = None
) -> None:
    owners = [
        decl
        for decl, cand in zip(decls, cands)
        if cand.get("file_scoped") and decl.parent is None
    ]
    if not owners and language in _PACKAGE_CLAIM_LANGUAGES:
        packages = [
            decl for decl in decls if decl.kind == "package" and decl.parent is None
        ]
        if len(packages) == 1:
            owners = packages
    if len(owners) != 1:
        return
    owner = owners[0]
    renamed: dict[str, str] = {}
    for decl in decls:
        if decl is owner or decl.parent is not None:
            continue
        old = decl.qualified_name
        short = old.rpartition(".")[2] or old
        decl.parent = owner.qualified_name
        decl.qualified_name = f"{owner.qualified_name}.{short}"
        decl.depth = owner.depth + 1
        renamed[old] = decl.qualified_name
    for decl in decls:
        if decl.parent in renamed:
            decl.parent = renamed[decl.parent]
            old = decl.qualified_name
            short = old.rpartition(".")[2] or old
            decl.qualified_name = f"{decl.parent}.{short}"
            renamed[old] = decl.qualified_name


def _reparent_by_owner(
    decls: list[ApiDeclRecord],
    cands: list[dict],
    impl_anchors: list[tuple[int, int, str]],
    module: str | None,
) -> None:
    depth_by_qual: dict[str, int] = {}
    for decl in decls:
        depth_by_qual.setdefault(decl.qualified_name, decl.depth)
    for decl, cand in zip(decls, cands):
        if decl.kind not in ("function", "method"):
            continue
        owner = cand.get("owner") or ""
        if not owner and impl_anchors:
            for start, end, anchor_owner in impl_anchors:
                if start <= decl.start_byte and decl.end_byte <= end:
                    owner = anchor_owner
                    break
        if not owner:
            continue
        base = f"{module}.{owner}" if module else owner
        name = decl.qualified_name.rpartition(".")[2] or decl.qualified_name
        decl.parent = base
        decl.qualified_name = f"{base}.{name}"
        decl.depth = depth_by_qual.get(base, 0) + 1
        if decl.kind == "function":
            decl.kind = "method"


def _build_aliases(
    cfg,
    source,
    dep_matches,
    module: str | None = None,
    importer_file: str | None = None,
) -> list[tuple[str, str]]:
    aliases: list[tuple[str, str]] = []
    for node, captures in _merge_matches(dep_matches):
        for alias, full in _rules.build_aliases(
            cfg.name, node, captures, source, module, importer_file
        ):
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
    if func and receiver is None and "." in func:
        head, _, tail = func.rpartition(".")
        if head and tail and "(" not in func and ")" not in func:
            return func, head, tail
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
    return (
        fn.type == "identifier" and _rules.slice_text(fn, source).strip() == "require"
    )


def _caller_for(node, decls: list[ApiDeclRecord]) -> str | None:
    best: ApiDeclRecord | None = None
    for decl in decls:
        if decl.kind not in ("function", "method", "constructor"):
            continue
        if not (decl.start_byte <= node.start_byte and node.end_byte <= decl.end_byte):
            continue
        if best is None or (
            decl.start_byte >= best.start_byte and decl.end_byte <= best.end_byte
        ):
            best = decl
    return best.qualified_name if best is not None else None
