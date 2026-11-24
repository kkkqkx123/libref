"""AST extraction orchestration: file to declaration and call records."""

from __future__ import annotations

from tree_sitter import QueryCursor

from libref.astminer import rules as _rules
from libref.astminer.c_shadow import shadow_for_parse
from libref.astminer.calls import _build_calls
from libref.astminer.config import LanguageConfig
from libref.astminer.context import determine_module_path
from libref.astminer.declarations import _build_decls, _comment_spans
from libref.astminer.models import ApiDeclRecord, RawCall
from libref.astminer.ownership import ScopedAlias, _build_aliases, _var_scope
from libref.astminer.parse import _compiled_queries, get_parser, warm_configs

__all__ = ["extract_file", "warm_configs"]


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
