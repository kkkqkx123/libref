"""Hybrid retrieval: FTS5 BM25 plus vector KNN, fusion and reranking."""

from __future__ import annotations

from libref.store.retriever.candidates import fts_search
from libref.store.retriever.engine import HybridRetriever, build_retriever
from libref.store.retriever.features import (
    ALIGNMENT_BONUS,
    _doc_feature_bonuses,
    alignment_key,
    filter_by_doc_facets,
)

__all__ = [
    "ALIGNMENT_BONUS",
    "HybridRetriever",
    "_doc_feature_bonuses",
    "alignment_key",
    "build_retriever",
    "filter_by_doc_facets",
    "fts_search",
]
