"""Generic extraction engine: query matching, deduplication/merging, record assembly."""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any

from tree_sitter import Parser, Query, QueryCursor

from libref.astminer import queries as _queries
from libref.astminer import rules as _rules
from libref.astminer.config import LanguageConfig, grammar_fingerprint, load_language
from libref.astminer.context import determine_module_path
from libref.astminer.models import ApiDeclRecord, RawCall
from libref.astminer.rules import CapturedItem, normalize_owner_text

_parsers: dict[str, tuple[str, Parser]] = {}

_compiled: dict[str, dict[str, Query]] = {}

_CLASS_LIKE = frozenset(
    {"class", "interface", "enum", "record", "annotation", "struct", "union"}
)

_NON_CALL_ROOTS = frozenset(
    {
        "jsx_opening_element",
        "jsx_self_closing_element",
        "explicit_constructor_invocation",
    }
)


@dataclass
class _Entity:
    """Assembly-stage entity: semantic identity resolved before record emit."""

    node: Any
    kind: str
    name: str
    items: list[CapturedItem] = field(default_factory=list)
    owner: str = ""
    file_scoped: bool = False
    parent: str | None = None
    depth: int = 0
    signature: str = ""
    params: list[str] = field(default_factory=list)
    param_types: list[str] = field(default_factory=list)
    return_type: str = ""
    bases: list[str] = field(default_factory=list)
    has_doc: bool = False
    span_override: tuple[int, int, int, int] | None = None
    impl_anchors: list[tuple[int, int, str]] = field(default_factory=list)


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


def _largest_valid(captures: dict[str, list]) -> tuple[str, Any] | None:
    best: tuple[str, Any] | None = None
    best_size = -1
    for name, nodes in captures.items():
        for node in nodes:
            if _rules.is_phantom(node):
                continue
            size = node.end_byte - node.start_byte
            if size > best_size:
                best_size = size
                best = (name, node)
    return best


def _merge_matches(matches, source: bytes) -> list[tuple[Any, list[CapturedItem]]]:
    merged: dict[tuple[int, int], tuple[Any, list[CapturedItem]]] = {}
    for _, captures in matches:
        pick = _largest_valid(captures)
        if pick is None:
            continue
        _, node = pick
        key = (node.start_byte, node.end_byte)
        items = _rules.to_items(captures, source)
        slot = merged.get(key)
        if slot is None:
            merged[key] = (node, items)
        else:
            slot[1].extend(items)
    return list(merged.values())


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
                if name.endswith(".doc"):
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
    entities = _collect_entities(entity_matches, source)
    anchors = _take_impl_anchors(entities)
    dep_spans = _dependency_spans(dep_matches, source)
    _nest_entities(source, entities, module, doc_spans, line_spans)
    _attach_bases_innermost(entities, dep_spans)
    _claim_file_scoped(entities, cfg.name)
    _reparent_by_owner(entities, anchors, module)
    _merge_partial_decls(entities)
    return [_emit_record(entity, path) for entity in entities]


def _collect_entities(entity_matches, source: bytes) -> list[_Entity]:
    cands: list[_Entity] = []
    for node, items in _merge_matches(entity_matches, source):
        roots = _rules.root_names(items)
        if "entity.impl" in roots:
            owner_node = _rules.first_node(items, ".type.name")
            owner = (
                normalize_owner_text(_rules.slice_text(owner_node, source))
                if owner_node is not None
                else ""
            )
            if owner:
                cands.append(
                    _Entity(
                        node=node,
                        kind="",
                        name="",
                        items=[],
                        impl_anchors=[(node.start_byte, node.end_byte, owner)],
                    )
                )
            continue
        kinds = {
            kind
            for kind in (_rules.entity_kind({root}) for root in roots)
            if kind is not None
        }
        kind = _rules.refine_kind(kinds)
        if kind is None:
            continue
        if kind == "class":
            for child in node.children:
                if child.type == "interface":
                    kind = "interface"
                    break
                if child.type == "enum":
                    kind = "enum"
                    break
        name = _rules.extract_entity_name(items, roots)
        if not name:
            continue
        owner_node = _rules.first_node(items, ".owner", ".receiver")
        owner = (
            normalize_owner_text(_rules.slice_text(owner_node, source))
            if owner_node is not None
            else ""
        )
        cands.append(
            _Entity(
                node=node,
                kind=kind,
                name=name,
                items=items,
                owner=owner,
                file_scoped=any(
                    item.name == "entity.package.file_scoped" for item in items
                ),
            )
        )
    cands.sort(key=lambda c: (c.node.start_byte, -c.node.end_byte))
    return cands


def _take_impl_anchors(entities: list[_Entity]) -> list[tuple[int, int, str]]:
    anchors: list[tuple[int, int, str]] = []
    kept = [entity for entity in entities if entity.kind]
    for entity in entities:
        anchors.extend(entity.impl_anchors)
    entities[:] = kept
    return anchors


def _dependency_spans(dep_matches, source: bytes) -> list[tuple[int, int, str]]:
    dep_spans: list[tuple[int, int, str]] = []
    for _, items in _merge_matches(dep_matches, source):
        for item in items:
            if item.name.endswith((".extend.name", ".implement.name")) and (
                item.node is not None
            ):
                text = _rules.generic_dotted(item.node, source).strip()
                if text:
                    dep_spans.append((item.start_byte, item.end_byte, text))
    dep_spans.sort()
    return dep_spans


def _nest_entities(source, entities, module, doc_spans, line_spans) -> None:
    stack: list[_Entity] = []
    for entity in entities:
        node = entity.node
        while stack and not (
            stack[-1].node.start_byte <= node.start_byte
            and node.end_byte <= stack[-1].node.end_byte
        ):
            stack.pop()
        parent = stack[-1] if stack else None
        entity.parent = parent.name if parent is not None else None
        entity.depth = len(stack)
        if (
            entity.kind == "function"
            and parent is not None
            and parent.kind in _CLASS_LIKE
        ):
            entity.kind = "method"
        if entity.kind == "package" and parent is None:
            qual = entity.name
        elif parent is not None:
            qual = f"{parent.name}.{entity.name}"
        elif module and (module == entity.name or module.endswith("." + entity.name)):
            qual = module
        elif module:
            qual = f"{module}.{entity.name}"
        else:
            qual = entity.name
        entity.name = qual
        body = _rules.first_node(entity.items, ".body")
        roots = _rules.root_names(entity.items)
        entity.signature = _rules.compose_signature(
            entity.items,
            source,
            node.start_byte,
            node.end_byte,
            body.start_byte if body is not None else None,
            _rules.roots_expect_body(roots),
        )
        params_node = _rules.first_node(entity.items, ".params", ".signature.params")
        entity.params = (
            _rules.params_from_params_node(params_node, source)
            if params_node is not None
            else []
        )
        entity.param_types = (
            _rules.param_types_from_params_node(params_node, source)
            if params_node is not None
            else []
        )
        return_node = _rules.first_node(
            entity.items, ".return_type", ".signature.return_type"
        )
        entity.return_type = _rules.shaped_return(return_node, source)
        entity.bases = _rules.extract_bases(entity.items)
        entity.has_doc = _has_doc(node, body, source, doc_spans, line_spans)
        stack.append(entity)


def _emit_record(entity: _Entity, path: str) -> ApiDeclRecord:
    node = entity.node
    start, end = node.start_byte, node.end_byte
    line_start, line_end = node.start_point[0] + 1, node.end_point[0] + 1
    if entity.span_override is not None:
        start, end, line_start, line_end = entity.span_override
    return ApiDeclRecord(
        qualified_name=entity.name,
        kind=entity.kind,
        file=path,
        line_start=line_start,
        line_end=line_end,
        start_byte=start,
        end_byte=end,
        signature=entity.signature,
        params=entity.params,
        param_types=entity.param_types,
        return_type=entity.return_type,
        bases=entity.bases,
        parent=entity.parent,
        depth=entity.depth,
        has_doc=entity.has_doc,
    )


def _attach_bases_innermost(
    entities: list[_Entity], dep_spans: list[tuple[int, int, str]]
) -> None:
    for start, end, text in dep_spans:
        best: _Entity | None = None
        best_size: int | None = None
        for entity in entities:
            node = entity.node
            if not (node.start_byte <= start and end <= node.end_byte):
                continue
            size = node.end_byte - node.start_byte
            if best is None or (best_size is not None and size < best_size):
                best, best_size = entity, size
        if best is not None and text not in best.bases:
            best.bases.append(text)


_PACKAGE_CLAIM_LANGUAGES = frozenset(
    {"java", "csharp", "kotlin", "groovy", "typescript"}
)

_PARTIAL_MERGE_KINDS = frozenset({"class", "interface", "struct", "record", "enum"})


def _merge_partial_decls(entities: list[_Entity]) -> None:
    first_at: dict[tuple[str | None, str], int] = {}
    drop: set[int] = set()
    for index, entity in enumerate(entities):
        if entity.kind not in _PARTIAL_MERGE_KINDS:
            continue
        key = (entity.parent, entity.name)
        prev = first_at.get(key)
        if prev is None:
            first_at[key] = index
            continue
        anchor = entities[prev]
        anchor_start, anchor_end = (
            anchor.span_override[:2]
            if anchor.span_override is not None
            else (anchor.node.start_byte, anchor.node.end_byte)
        )
        anchor_lines = (
            anchor.span_override[2:]
            if anchor.span_override is not None
            else (anchor.node.start_point[0] + 1, anchor.node.end_point[0] + 1)
        )
        start = min(anchor_start, entity.node.start_byte)
        end = max(anchor_end, entity.node.end_byte)
        line_start = min(anchor_lines[0], entity.node.start_point[0] + 1)
        line_end = max(anchor_lines[1], entity.node.end_point[0] + 1)
        anchor.span_override = (start, end, line_start, line_end)
        for base in entity.bases:
            if base not in anchor.bases:
                anchor.bases.append(base)
        anchor.has_doc = anchor.has_doc or entity.has_doc
        if not anchor.signature and entity.signature:
            anchor.signature = entity.signature
        drop.add(index)
    for index in sorted(drop, reverse=True):
        del entities[index]


def _claim_file_scoped(entities: list[_Entity], language: str | None = None) -> None:
    owners = [
        entity for entity in entities if entity.file_scoped and entity.parent is None
    ]
    if not owners and language in _PACKAGE_CLAIM_LANGUAGES:
        packages = [
            entity
            for entity in entities
            if entity.kind == "package" and entity.parent is None
        ]
        if len(packages) == 1:
            owners = packages
    if len(owners) != 1:
        return
    owner = owners[0]
    renamed: dict[str, str] = {}
    for entity in entities:
        if entity is owner or entity.parent is not None:
            continue
        old = entity.name
        short = old.rpartition(".")[2] or old
        entity.parent = owner.name
        entity.name = f"{owner.name}.{short}"
        entity.depth = owner.depth + 1
        renamed[old] = entity.name
    for entity in entities:
        if entity.parent in renamed:
            entity.parent = renamed[entity.parent]
            old = entity.name
            short = old.rpartition(".")[2] or old
            entity.name = f"{entity.parent}.{short}"
            renamed[old] = entity.name


def _reparent_by_owner(
    entities: list[_Entity],
    impl_anchors: list[tuple[int, int, str]],
    module: str | None,
) -> None:
    depth_by_qual: dict[str, int] = {}
    for entity in entities:
        depth_by_qual.setdefault(entity.name, entity.depth)
    for entity in entities:
        if entity.kind not in ("function", "method"):
            continue
        owner = entity.owner or ""
        if not owner and impl_anchors:
            for start, end, anchor_owner in impl_anchors:
                if start <= entity.node.start_byte and entity.node.end_byte <= end:
                    owner = anchor_owner
                    break
        if not owner:
            continue
        base = f"{module}.{owner}" if module else owner
        short = entity.name.rpartition(".")[2] or entity.name
        entity.parent = base
        entity.name = f"{base}.{short}"
        entity.depth = depth_by_qual.get(base, 0) + 1
        if entity.kind == "function":
            entity.kind = "method"


def _build_aliases(
    cfg,
    source,
    dep_matches,
    module: str | None = None,
    importer_file: str | None = None,
) -> list[tuple[str, str]]:
    aliases: list[tuple[str, str]] = []
    for node, items in _merge_matches(dep_matches, source):
        for alias, full in _rules.build_aliases(
            cfg.name, node, items, source, module, importer_file
        ):
            if alias and full:
                aliases.append((alias, full))
    return aliases


def _build_calls(path, cfg, source, call_matches, decls) -> list[RawCall]:
    groups = _merge_matches(call_matches, source)
    calls: list[RawCall] = []
    for node, items in groups:
        if node.type in _NON_CALL_ROOTS:
            continue
        if node.type not in _rules.CALL_NODE_TYPES:
            continue
        if _is_module_loader(node, source):
            continue
        raw, receiver, func = _rules.extract_call_parts(items, node, source)
        if not raw:
            continue
        args_node = _rules.first_node(items, ".arguments")
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
