"""Language rule functions: text shaping, entity selection, alias building."""

from __future__ import annotations

from libref.astminer.rules.aliases import (
    _csharp_aliases,
    _go_aliases,
    _java_aliases,
    _rust_aliases,
    build_aliases,
    normalize_go_import,
    resolve_rust_use_path,
)
from libref.astminer.rules.entities import (
    CALLABLE_DECL_KINDS,
    all_texts,
    collect_simple_var_aliases,
    entity_kind,
    first_node,
    refine_kind,
)
from libref.astminer.rules.text import (
    BUILTIN_BARE_BY_LANGUAGE,
    BUILTIN_BARE_CALLS,
    CALL_NODE_TYPES,
    count_arguments,
    generic_dotted,
    is_builtin_bare,
    normalize_owner_text,
    param_name,
    param_types_from_params_node,
    params_from_params_node,
    shaped_return,
    slice_text,
    summarize_arg,
    summarize_arguments,
    type_text_of_param,
)

__all__ = [
    "BUILTIN_BARE_BY_LANGUAGE",
    "BUILTIN_BARE_CALLS",
    "CALLABLE_DECL_KINDS",
    "CALL_NODE_TYPES",
    "_csharp_aliases",
    "_go_aliases",
    "_java_aliases",
    "_rust_aliases",
    "all_texts",
    "build_aliases",
    "collect_simple_var_aliases",
    "count_arguments",
    "entity_kind",
    "first_node",
    "generic_dotted",
    "is_builtin_bare",
    "normalize_go_import",
    "normalize_owner_text",
    "param_name",
    "param_types_from_params_node",
    "params_from_params_node",
    "refine_kind",
    "resolve_rust_use_path",
    "shaped_return",
    "slice_text",
    "summarize_arg",
    "summarize_arguments",
    "type_text_of_param",
]
