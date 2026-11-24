"""Parser, query cache and AST traversal primitives."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from tree_sitter import Parser, Query

from libref.astminer import queries as _queries
from libref.astminer import rules as _rules
from libref.astminer.config import LanguageConfig, grammar_fingerprint, load_language
from libref.astminer.rules import CapturedItem

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
