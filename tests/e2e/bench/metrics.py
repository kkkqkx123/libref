"""Retrieval benchmark metrics: pure ranking math over ranked hit flags.

Every function takes per-query binary relevance (one relevant page per query)
so the numbers stay comparable across the doc and code corpora.
"""

from __future__ import annotations

import math
from typing import Any


def first_hit_rank(flags: list[bool], k: int) -> int:
    """1-based rank of the first relevant hit inside the top k; 0 when absent."""
    for index, hit in enumerate(flags[: max(0, int(k))]):
        if hit:
            return index + 1
    return 0


def reciprocal_rank(rank: int) -> float:
    """MRR contribution: 1/rank, zero for a miss."""
    return 1.0 / rank if rank > 0 else 0.0


def ndcg(rank: int, k: int) -> float:
    """nDCG contribution with binary gain and a single relevant item."""
    if rank <= 0 or rank > k:
        return 0.0
    return 1.0 / math.log2(rank + 1)


def summarize(per_query: list[dict[str, Any]], k: int = 5) -> dict[str, Any]:
    """Aggregate per-query rank rows into recall/MRR/nDCG plus miss and duplicate rates."""
    total = len(per_query)
    if total == 0:
        return {
            "queries": 0,
            "recall_at_k": 0.0,
            "mrr_at_k": 0.0,
            "ndcg_at_k": 0.0,
            "empty_rate": 0.0,
            "duplicate_rate": 0.0,
        }
    ranks = [int(row.get("rank", 0)) for row in per_query]
    return {
        "queries": total,
        "recall_at_k": sum(1 for r in ranks if r > 0) / total,
        "mrr_at_k": sum(reciprocal_rank(r) for r in ranks) / total,
        "ndcg_at_k": sum(ndcg(r, k) for r in ranks) / total,
        "empty_rate": sum(1 for row in per_query if row.get("count", 0) == 0) / total,
        "duplicate_rate": sum(int(row.get("duplicates", 0)) for row in per_query)
        / max(1, sum(int(row.get("count", 0)) for row in per_query)),
    }


def distribution(values: list[float]) -> dict[str, float]:
    """Min/median/max of a score list, for spotting ranking-scale drift."""
    if not values:
        return {"min": 0.0, "median": 0.0, "max": 0.0}
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2.0
    )
    return {"min": ordered[0], "median": median, "max": ordered[-1]}
