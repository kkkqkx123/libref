"""Statistics and sequence mining (pure in-memory algorithms; persistence is orchestrated by services)."""

from .cluster import cluster_apis
from .cooccur import build_scope_sets, cooccur_matrix, scope_key
from .facets import (
    FACET_ARG_SHAPE,
    FACET_CALL_ORDER,
    FACET_CALL_SHAPE,
    FACET_COMBO,
    FACET_GUARD,
    aggregate_facets,
    expand_candidates,
    facet_callee,
    mask_call_shape,
    order_bucket,
    score_facet_row,
    select_facets,
    split_shape_value,
)
from .feature_tree import build_feature_tree
from .frequency import exact_counts, level_counts, prune_to_top_n, top_exact_names
from .idiom import (
    deduplicate_evidence,
    format_short_signature,
    mask_arg,
    neighbour_keep,
    normalize_signature,
    reuse_diversity,
    synthesize_snippet,
)
from .induction import (
    InductionInput,
    PatternLabel,
    build_label_prompt,
    parse_label_payload,
    pattern_hash,
)
from .normalize import build_table, decode_pattern, encode_sequences, reverse_table
from .prefixspan import PrefixSpan
from .scoring import (
    cluster_hit,
    cluster_hit_exact,
    content_idents,
    name_hit,
    support_for_content,
    support_for_entities,
)

__all__ = [
    "FACET_ARG_SHAPE",
    "FACET_CALL_ORDER",
    "FACET_CALL_SHAPE",
    "FACET_COMBO",
    "FACET_GUARD",
    "InductionInput",
    "PatternLabel",
    "PrefixSpan",
    "aggregate_facets",
    "build_feature_tree",
    "build_label_prompt",
    "build_scope_sets",
    "build_table",
    "cluster_apis",
    "cluster_hit",
    "cluster_hit_exact",
    "content_idents",
    "cooccur_matrix",
    "decode_pattern",
    "deduplicate_evidence",
    "encode_sequences",
    "exact_counts",
    "expand_candidates",
    "facet_callee",
    "format_short_signature",
    "level_counts",
    "mask_arg",
    "mask_call_shape",
    "name_hit",
    "neighbour_keep",
    "normalize_signature",
    "order_bucket",
    "parse_label_payload",
    "pattern_hash",
    "prune_to_top_n",
    "reuse_diversity",
    "reverse_table",
    "scope_key",
    "score_facet_row",
    "select_facets",
    "split_shape_value",
    "support_for_content",
    "support_for_entities",
    "synthesize_snippet",
    "top_exact_names",
]
