"""Hybrid retrieval: FTS5 BM25 plus vector KNN, fusion and reranking."""

from __future__ import annotations

from libref.store.retriever.candidates import fts_search
from libref.store.retriever.engine import HybridRetriever, build_retriever
from libref.store.retriever.features import (
    alignment_key,
    doc_feature_annotations,
    filter_by_doc_facets,
)

__all__ = [
    "HybridRetriever",
    "alignment_key",
    "build_retriever",
    "doc_feature_annotations",
    "filter_by_doc_facets",
    "fts_search",
]
