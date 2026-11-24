"""Declaration extraction and record emission."""

from __future__ import annotations

import bisect

from libref.astminer import queries as _queries
from libref.astminer import rules as _rules
from libref.astminer.macro_calls import (
    macro_body_hash,
    macro_body_signature,
)
from libref.astminer.models import ApiDeclRecord
from libref.astminer.ownership import (
    _PARTIAL_MERGE_KINDS,
    _normalize_ownership,
)
from libref.astminer.parse import _Entity, _merge_matches, _walk, node_text
from libref.astminer.rules import normalize_owner_text


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
