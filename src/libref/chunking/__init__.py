"""分块层：双路预算分块与计量原语。"""

from libref.chunking.budget import (
    ChunkBudget,
    ChunkPath,
    cost,
    count_terms,
    default_budgets,
    estimate_tokens,
    truncate_to_cost,
)
from libref.chunking.splitter import (
    Chunk,
    DualChunks,
    ensure_hard_limit,
    split_markdown,
    split_markdown_dual,
)

__all__ = [
    "Chunk",
    "ChunkBudget",
    "ChunkPath",
    "DualChunks",
    "cost",
    "count_terms",
    "default_budgets",
    "ensure_hard_limit",
    "estimate_tokens",
    "split_markdown",
    "split_markdown_dual",
    "truncate_to_cost",
]
