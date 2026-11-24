"""Generic extraction engine: query matching, deduplication/merging, record assembly."""

from __future__ import annotations

import bisect
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from tree_sitter import Parser, Query, QueryCursor

from libref.astminer import queries as _queries
from libref.astminer import rules as _rules
from libref.astminer.c_shadow import shadow_for_parse
from libref.astminer.config import LanguageConfig, grammar_fingerprint, load_language
from libref.astminer.context import determine_module_path
from libref.astminer.macro_calls import (
    find_macro_calls,
    macro_body_hash,
    macro_body_signature,
)
from libref.astminer.models import ApiDeclRecord, RawCall
from libref.astminer.rules import CapturedItem, normalize_owner_text

_parsers: dict[str, tuple[str, Parser]] = {}

_compiled: dict[str, dict[str, Query]] = {}


@dataclass
class _Entity:
    """Assembly-stage entity: semantic identity resolved before record emit."""

    node: Any
    kind: str
    name: str
    short: str = ""
    structural_parent: int | None = None
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
    preproc_conditions: list[str] = field(default_factory=list)


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


def warm_configs(configs: Iterable[LanguageConfig]) -> None:
    """Prepare parsers and queries for the languages a worker task actually uses."""
    for cfg in configs:
        get_parser(cfg)
        _compiled_queries(cfg)


def node_text(node, source: bytes) -> str:
    return _rules.slice_text(node, source)


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


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


ScopedAlias = tuple[str, str, str | None]

_CALLABLE_SCOPE_KINDS = frozenset({"function", "method", "constructor"})
_CLASS_SCOPE_KINDS = frozenset(
    {"class", "interface", "enum", "record", "annotation", "struct", "union"}
)
_SELF_SCOPED_HEADS = frozenset({"self", "cls", "this", "Self"})


def extract_file(
    path: str, cfg: LanguageConfig, source: bytes
) -> tuple[list[ApiDeclRecord], list[ScopedAlias], list[RawCall]]:
    parser = get_parser(cfg)
    # The tree is built from shadow bytes with identical spans; all text is
    # still sliced from the original source, so evidence stays verbatim.
    tree = parser.parse(shadow_for_parse(source, cfg.name))
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
        root=tree.root_node,
    )
    alias_module = module
    if module is not None and path.replace("\\", "/").endswith("__init__.py"):
        alias_module = f"{module}.__init__"
    aliases = _build_aliases(cfg, source, dep_matches, alias_module, path)
    for var, target, at in _rules.collect_simple_var_aliases(tree.root_node, source):
        if not var or not target:
            continue
        aliases.append((var, target, _var_scope(decls, var, at)))
    calls = _build_calls(path, cfg, source, call_matches, decls, tree.root_node)
    return decls, aliases, calls


def _preproc_conditions(dep_matches, source: bytes) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    for node, items in _merge_matches(dep_matches, source):
        names = list(
            dict.fromkeys(
                item.text
                for item in items
                if _queries.is_preproc_condition(item.name) and item.text
            )
        )
        if names:
            found.append((node.start_byte, node.end_byte, ",".join(names)))
    found.sort()
    return found


def _comment_spans(
    comment_matches,
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    doc: list[tuple[int, int]] = []
    line: list[tuple[int, int]] = []
    for _, captures in comment_matches:
        for name, nodes in captures.items():
            for node in nodes:
                span = (node.start_byte, node.end_byte)
                if _queries.is_comment_doc(name):
                    doc.append(span)
                elif _queries.is_comment_line(name):
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
    root=None,
) -> list[ApiDeclRecord]:
    doc_spans = list(doc_nodes or [])
    line_spans = list(line_nodes or [])
    entities = _collect_entities(entity_matches, source)
    anchors = _take_impl_anchors(entities)
    dep_spans = _dependency_spans(dep_matches, source)
    _nest_structure(entities)
    _classify_all(entities)
    _fill_details(source, entities, doc_spans, line_spans)
    _attach_preproc_conditions(entities, _preproc_conditions(dep_matches, source))
    _attach_bases_innermost(entities, dep_spans)
    _merge_partial_decls(entities)
    _normalize_ownership(entities, anchors, module, cfg.name)
    records = [_emit_record(entity, path, source) for entity in entities]
    if root is not None and cfg.name in ("c", "cpp"):
        records.extend(
            _attribute_declarations(
                root,
                source,
                path,
                dep_matches,
                {item.qualified_name for item in records},
            )
        )
    return records


def _collect_entities(entity_matches, source: bytes) -> list[_Entity]:
    cands: list[_Entity] = []
    for node, items in _merge_matches(entity_matches, source):
        roots = _rules.root_names(items)
        if any(_queries.is_impl_root(root) for root in roots):
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
                        short="",
                        structural_parent=None,
                        items=[],
                        impl_anchors=[(node.start_byte, node.end_byte, owner)],
                    )
                )
            continue
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
                kind="",
                name="",
                short=name,
                structural_parent=None,
                items=items,
                owner=owner,
                file_scoped=any(
                    _queries.is_file_scoped_capture(item.name) for item in items
                ),
            )
        )
    cands.sort(key=lambda c: (c.node.start_byte, -c.node.end_byte))
    return cands


def _take_impl_anchors(entities: list[_Entity]) -> list[tuple[int, int, str]]:
    anchors: list[tuple[int, int, str]] = []
    for entity in entities:
        anchors.extend(entity.impl_anchors)
    entities[:] = [entity for entity in entities if entity.items]
    return anchors


def _dependency_spans(dep_matches, source: bytes) -> list[tuple[int, int, str]]:
    dep_spans: list[tuple[int, int, str]] = []
    for _, items in _merge_matches(dep_matches, source):
        for item in items:
            if _queries.is_base_name_capture(item.name) and (item.node is not None):
                text = _rules.generic_dotted(item.node, source).strip()
                if text:
                    dep_spans.append((item.start_byte, item.end_byte, text))
    dep_spans.sort()
    return dep_spans


def _nest_structure(entities: list[_Entity]) -> None:
    stack: list[int] = []
    for index, entity in enumerate(entities):
        node = entity.node
        while stack and not (
            entities[stack[-1]].node.start_byte <= node.start_byte
            and node.end_byte <= entities[stack[-1]].node.end_byte
        ):
            stack.pop()
        entity.structural_parent = stack[-1] if stack else None
        stack.append(index)


def _classify_all(entities: list[_Entity]) -> None:
    drop: set[int] = set()
    for index, entity in enumerate(entities):
        parent_kind: str | None = None
        if entity.structural_parent is not None:
            parent_kind = entities[entity.structural_parent].kind or None
        kind = _rules.classify_entity_kind(
            _rules.root_names(entity.items), entity.node, parent_kind
        )
        if kind is None:
            drop.add(index)
        else:
            entity.kind = kind
    if not drop:
        return
    remap: dict[int, int | None] = {}
    kept: list[_Entity] = []
    for old, entity in enumerate(entities):
        if old in drop:
            remap[old] = None
        else:
            remap[old] = len(kept)
            kept.append(entity)
    for entity in kept:
        parent = entity.structural_parent
        while parent is not None and remap.get(parent) is None:
            parent = entities[parent].structural_parent
        entity.structural_parent = remap.get(parent) if parent is not None else None
    entities[:] = kept


def _fill_details(source, entities, doc_spans, line_spans) -> None:
    for entity in entities:
        node = entity.node
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


def _attach_preproc_conditions(
    entities: list[_Entity], conditions: list[tuple[int, int, str]]
) -> None:
    for start, end, text in conditions:
        if not text:
            continue
        best: _Entity | None = None
        best_size: int | None = None
        for entity in entities:
            node = entity.node
            if not (start <= node.start_byte <= end):
                continue
            size = node.end_byte - node.start_byte
            if best_size is None or size < best_size:
                best, best_size = entity, size
        if best is not None and text not in best.preproc_conditions:
            best.preproc_conditions.append(text)


def _emit_record(entity: _Entity, path: str, source: bytes) -> ApiDeclRecord:
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
        preproc_conditions=list(entity.preproc_conditions),
        macro_body_signature=(
            macro_body_signature(node_text(node, source))
            if entity.kind == "macro"
            else ""
        ),
        macro_body_hash=(
            macro_body_hash(node_text(node, source)) if entity.kind == "macro" else ""
        ),
    )


def _attribute_declarations(
    root,
    source: bytes,
    path: str,
    dep_matches,
    existing: set[str],
) -> list[ApiDeclRecord]:
    """Recover prototypes whose GNU attributes leave a function declarator in an ERROR node."""
    records: list[ApiDeclRecord] = []
    conditions = _preproc_conditions(dep_matches, source)
    for node in _walk(root):
        if node.type != "ERROR":
            continue
        declaration = node.parent
        while declaration is not None and declaration.type != "declaration":
            declaration = declaration.parent
        if declaration is None:
            continue
        text = " ".join(node_text(declaration, source).split())
        if not text.endswith(";"):
            continue
        for candidate in _walk(node):
            if candidate.type != "function_declarator":
                continue
            name_node = candidate.child_by_field_name("declarator")
            params_node = candidate.child_by_field_name("parameters")
            if name_node is None or name_node.type != "identifier":
                continue
            name = node_text(name_node, source).strip()
            if not name or name in existing:
                continue
            entity = _Entity(
                node=declaration,
                kind="function",
                name=name,
                short=name,
                items=[],
                signature=text[:-1].strip(),
                params=(
                    _rules.params_from_params_node(params_node, source)
                    if params_node is not None
                    else []
                ),
            )
            _attach_preproc_conditions([entity], conditions)
            records.append(_emit_record(entity, path, source))
            existing.add(name)
    return records


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
    first_at: dict[tuple[int | None, str], int] = {}
    repoint: dict[int, int] = {}
    drop: set[int] = set()
    for index, entity in enumerate(entities):
        if entity.kind not in _PARTIAL_MERGE_KINDS:
            continue
        key = (entity.structural_parent, entity.short)
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
        repoint[index] = prev
        drop.add(index)
    if drop:
        for entity in entities:
            parent = entity.structural_parent
            if parent is not None and parent in repoint:
                entity.structural_parent = repoint[parent]
        kept: list[_Entity] = []
        remap: dict[int, int] = {}
        for old, entity in enumerate(entities):
            if old in drop:
                continue
            remap[old] = len(kept)
            kept.append(entity)
        for entity in kept:
            if entity.structural_parent is not None:
                entity.structural_parent = remap.get(entity.structural_parent)
        entities[:] = kept


def _normalize_ownership(
    entities: list[_Entity],
    impl_anchors: list[tuple[int, int, str]],
    module: str | None,
    language: str | None = None,
) -> None:
    top = [i for i, e in enumerate(entities) if e.structural_parent is None]
    scoped = [i for i in top if entities[i].file_scoped]
    owner_idx: int | None = None
    if len(scoped) == 1:
        owner_idx = scoped[0]
    elif language in _PACKAGE_CLAIM_LANGUAGES:
        pkgs = [i for i in top if entities[i].kind == "package"]
        if len(pkgs) == 1:
            owner_idx = pkgs[0]
    owner_qual = entities[owner_idx].short if owner_idx is not None else None
    depth_by_qual: dict[str, int] = {}
    final_qual: dict[int, str] = {}
    for index, entity in enumerate(entities):
        owner_name = ""
        if entity.kind in ("function", "method"):
            owner_name = entity.owner or ""
            if not owner_name and impl_anchors:
                for start, end, anchor_owner in impl_anchors:
                    if start <= entity.node.start_byte and entity.node.end_byte <= end:
                        owner_name = anchor_owner
                        break
        if owner_name:
            base = f"{module}.{owner_name}" if module else owner_name
            entity.parent = base
            entity.name = f"{base}.{entity.short}"
            entity.depth = depth_by_qual.get(base, 0) + 1
            if entity.kind == "function":
                entity.kind = "method"
            depth_by_qual.setdefault(entity.name, entity.depth)
            final_qual[index] = entity.name
            continue
        parent_idx = entity.structural_parent
        if parent_idx is None:
            if owner_idx is not None and index != owner_idx:
                entity.parent = owner_qual
                entity.name = f"{owner_qual}.{entity.short}"
                entity.depth = 1
            elif entity.kind == "package":
                entity.parent = None
                entity.name = entity.short
                entity.depth = 0
            elif module and (
                module == entity.short or module.endswith("." + entity.short)
            ):
                entity.parent = None
                entity.name = module
                entity.depth = 0
            elif module:
                entity.parent = None
                entity.name = f"{module}.{entity.short}"
                entity.depth = 0
            else:
                entity.parent = None
                entity.name = entity.short
                entity.depth = 0
            depth_by_qual.setdefault(entity.name, entity.depth)
            final_qual[index] = entity.name
            continue
        parent_qual = final_qual.get(parent_idx)
        if parent_qual is None:
            parent_qual = entities[parent_idx].name or entities[parent_idx].short
        entity.parent = parent_qual
        entity.name = f"{parent_qual}.{entity.short}"
        entity.depth = depth_by_qual.get(parent_qual, 0) + 1
        depth_by_qual.setdefault(entity.name, entity.depth)
        final_qual[index] = entity.name


def _build_aliases(
    cfg,
    source,
    dep_matches,
    module: str | None = None,
    importer_file: str | None = None,
) -> list[ScopedAlias]:
    aliases: list[ScopedAlias] = []
    for node, items in _merge_matches(dep_matches, source):
        for alias, full in _rules.build_aliases(
            cfg.name, node, items, source, module, importer_file
        ):
            if alias and full:
                aliases.append((alias, full, None))
    return aliases


def _innermost_qual(
    decls: list[ApiDeclRecord], at: int, kinds: frozenset
) -> str | None:
    best: ApiDeclRecord | None = None
    for decl in decls:
        if decl.kind not in kinds:
            continue
        if not (decl.start_byte <= at < decl.end_byte):
            continue
        if best is None or (
            decl.start_byte >= best.start_byte and decl.end_byte <= best.end_byte
        ):
            best = decl
    return best.qualified_name if best is not None else None


def _var_scope(decls: list[ApiDeclRecord], var: str, at: int) -> str:
    head = var.partition(".")[0]
    if head in _SELF_SCOPED_HEADS:
        enclosing = _innermost_qual(decls, at, _CLASS_SCOPE_KINDS)
        return enclosing if enclosing is not None else ""
    enclosing = _innermost_qual(decls, at, _CALLABLE_SCOPE_KINDS)
    return enclosing if enclosing is not None else ""


_RELATION_PRIORITY = {
    "macro": 7,
    "method": 6,
    "constructor": 6,
    "pointer": 5,
    "direct": 4,
    "chained": 3,
    "callback": 2,
    "funcref": 1,
}


def _relation_kind(items: list, node, source: bytes) -> str:
    found = [
        kind
        for item in items
        if (kind := _queries.relation_for_capture(item.name)) is not None
    ]
    if node.type == "macro_invocation":
        found.append("macro")
    if not found:
        return "direct"
    return max(found, key=lambda kind: _RELATION_PRIORITY.get(kind, 0))


def _dedupe_calls(calls: list[RawCall]) -> list[RawCall]:
    """Collapse overlapping hits on the same call site from several patterns."""
    kept: list[RawCall] = []
    ordered = sorted(calls, key=lambda c: (c.caller_file, c.caller or "", c.start_byte))
    for call in ordered:
        tail = call.func or call.raw_callee.rpartition(".")[2]
        size = call.end_byte - call.start_byte
        merged = False
        for index, other in enumerate(kept):
            if other.caller_file != call.caller_file or other.caller != call.caller:
                continue
            other_tail = other.func or other.raw_callee.rpartition(".")[2]
            if tail != other_tail:
                continue
            other_size = other.end_byte - other.start_byte
            larger = max(size, other_size)
            smaller = min(size, other_size)
            if larger > smaller * 2 or smaller == 0:
                continue
            overlap = min(call.end_byte, other.end_byte) - max(
                call.start_byte, other.start_byte
            )
            if overlap < smaller * 0.8:
                continue
            rank = _RELATION_PRIORITY.get(call.relation_kind, 0)
            other_rank = _RELATION_PRIORITY.get(other.relation_kind, 0)
            if (rank, -size) > (other_rank, -other_size):
                kept[index] = call
            merged = True
            break
        if not merged:
            kept.append(call)
    return kept


def _build_calls(path, cfg, source, call_matches, decls, root=None) -> list[RawCall]:
    groups = _merge_matches(call_matches, source)
    calls: list[RawCall] = []
    for node, items in groups:
        shapes = [_queries.is_call_capture(item.name) for item in items]
        if node.type not in _rules.CALL_NODE_TYPES and not any(shapes):
            continue
        if _is_module_loader(node, source):
            continue
        raw, receiver, func = _rules.extract_call_parts(items, node, source)
        if not raw:
            continue
        relation = _relation_kind(items, node, source)
        if relation != "macro" and cfg.name in ("c", "cpp"):
            macro_names = {
                decl.qualified_name.rpartition(".")[2]
                for decl in decls
                if decl.kind == "macro"
            }
            if (func or raw).rpartition(".")[2] in macro_names:
                relation = "macro"
        args_node = _rules.first_node(items, ".arguments")
        if args_node is None:
            args_node = node.child_by_field_name("arguments")
        if args_node is None:
            for child in node.children:
                if child.type in ("argument_list", "arguments", "token_tree"):
                    args_node = child
                    break
        args = (
            _rules.summarize_arguments(args_node, source)
            if args_node is not None
            and args_node.type in ("argument_list", "arguments")
            else []
        )
        count = (
            _rules.count_arguments(args_node)
            if args_node is not None
            and args_node.type in ("argument_list", "arguments")
            else 0
        )
        calls.append(
            RawCall(
                raw_callee=raw,
                args=args,
                caller_file=path,
                caller=_caller_for(node, decls),
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                receiver=receiver,
                func=func,
                argument_count=count,
                relation_kind=relation,
            )
        )
    if root is not None:
        for macro in find_macro_calls(root, source, cfg.name, path, get_parser(cfg)):
            calls.append(
                RawCall(
                    raw_callee=macro.name,
                    args=list(macro.args),
                    caller_file=path,
                    caller=_caller_at(decls, macro.start_byte),
                    line_start=macro.line_start,
                    line_end=macro.line_end,
                    start_byte=macro.start_byte,
                    end_byte=macro.end_byte,
                    func=macro.name,
                    relation_kind="macro",
                    macro_generated=True,
                    macro_host=macro.host_macro,
                    macro_host_file=macro.host_file or path,
                    macro_definition_line=macro.macro_definition_line,
                    macro_nesting=macro.macro_nesting,
                    macro_conditions=list(macro.macro_conditions),
                    macro_body_kind=macro.macro_body_kind,
                    macro_body_signature=macro.macro_body_signature,
                )
            )
    return _dedupe_calls(calls)


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
    return _caller_at(decls, node.start_byte, node.end_byte)


def _caller_at(
    decls: list[ApiDeclRecord], start: int, end: int | None = None
) -> str | None:
    best: ApiDeclRecord | None = None
    for decl in decls:
        if decl.kind not in ("function", "method", "constructor"):
            continue
        if not (decl.start_byte <= start and (end is None or end <= decl.end_byte)):
            continue
        if best is None or (
            decl.start_byte >= best.start_byte and decl.end_byte <= best.end_byte
        ):
            best = decl
    return best.qualified_name if best is not None else None
