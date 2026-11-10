"""Statistics and sequence mining (pure in-memory algorithms; persistence is orchestrated by services)."""

from .cluster import cluster_apis
from .cooccur import build_scope_sets, cooccur_matrix, scope_key
from .feature_tree import build_feature_tree
from .frequency import exact_counts, level_counts, prune_to_top_n, top_exact_names
from .idiom import (
    deduplicate_evidence,
    mask_arg,
    normalize_signature,
    reuse_diversity,
    synthesize_snippet,
)
from .induction import (
    InductionInput,
    PatternLabel,
    build_label_prompt,
    build_taxonomy_prompt,
    merge_taxonomy,
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
    "InductionInput",
    "PatternLabel",
    "PrefixSpan",
    "build_feature_tree",
    "build_label_prompt",
    "build_scope_sets",
    "build_table",
    "build_taxonomy_prompt",
    "cluster_apis",
    "cluster_hit",
    "cluster_hit_exact",
    "content_idents",
    "cooccur_matrix",
    "decode_pattern",
    "deduplicate_evidence",
    "encode_sequences",
    "exact_counts",
    "level_counts",
    "mask_arg",
    "merge_taxonomy",
    "name_hit",
    "normalize_signature",
    "parse_label_payload",
    "pattern_hash",
    "prune_to_top_n",
    "reuse_diversity",
    "reverse_table",
    "scope_key",
    "support_for_content",
    "support_for_entities",
    "synthesize_snippet",
    "top_exact_names",
]
