"""Chunking layer: dual-path budgeted chunking and metering primitives."""

from libref.chunking.budget import (
    ChunkBudget,
    ChunkPath,
    budgets_from_settings,
    cost,
    count_terms,
    default_budgets,
    estimate_tokens,
    truncate_to_cost,
)
from libref.chunking.splitter import (
    Chunk,
    DualChunks,
    chunk_content_hash,
    ensure_hard_limit,
    extract_keywords,
    split_markdown,
    split_markdown_dual,
)
from libref.text.bm25 import clean_text_for_bm25
from libref.text.ident import ident_snake_form

__all__ = [
    "Chunk",
    "chunk_content_hash",
    "clean_text_for_bm25",
    "extract_keywords",
    "ident_snake_form",
    "ChunkBudget",
    "ChunkPath",
    "DualChunks",
    "cost",
    "count_terms",
    "budgets_from_settings",
    "default_budgets",
    "ensure_hard_limit",
    "estimate_tokens",
    "split_markdown",
    "split_markdown_dual",
    "truncate_to_cost",
]
