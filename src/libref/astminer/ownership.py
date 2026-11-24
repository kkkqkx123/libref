"""Declaration ownership and alias normalization."""

from __future__ import annotations

from libref.astminer import queries as _queries
from libref.astminer import rules as _rules
from libref.astminer.models import ApiDeclRecord
from libref.astminer.parse import _Entity, _merge_matches

_PACKAGE_CLAIM_LANGUAGES = frozenset(
    {"java", "csharp", "kotlin", "groovy", "typescript"}
)


_PARTIAL_MERGE_KINDS = frozenset({"class", "interface", "struct", "record", "enum"})


ScopedAlias = tuple[str, str, str | None]


_CALLABLE_SCOPE_KINDS = frozenset({"function", "method", "constructor"})


_CLASS_SCOPE_KINDS = frozenset(
    {"class", "interface", "enum", "record", "annotation", "struct", "union"}
)


_SELF_SCOPED_HEADS = frozenset({"self", "cls", "this", "Self"})


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
