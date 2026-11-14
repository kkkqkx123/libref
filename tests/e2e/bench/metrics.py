"""Retrieval benchmark metrics: pure ranking math over per-query graded hits.

Each per-query row carries the graded relevance of the returned results: grade 2
when the row is the target section the query names, grade 1 when it is the target
page but a different section, 0 otherwise. One relevant item per query keeps the
numbers comparable across the doc and code corpora; the grade distinguishes "the
answer section" from "the right page, wrong section".
"""

from __future__ import annotations

import math
import random
from typing import Any

# Paired resampling draws this many replicates unless a caller asks otherwise: enough
# for a stable 95% interval at a few hundred queries, cheap enough for every report.
BOOTSTRAP_ITERS = 2000
BOOTSTRAP_SEED = 7


def first_hit_rank(flags: list[bool], k: int) -> int:
    """1-based rank of the first relevant hit inside the top k; 0 when absent."""
    for index, hit in enumerate(flags[: max(0, int(k))]):
        if hit:
            return index + 1
    return 0


def reciprocal_rank(rank: int) -> float:
    """MRR contribution: 1/rank, zero for a miss."""
    return 1.0 / rank if rank > 0 else 0.0


def _gain(grade: int) -> float:
    return float(2 ** int(grade) - 1)


def graded_ndcg(gains: list[int], best: int, k: int) -> float:
    """nDCG with exponential gains against the best single hit the judgment allows."""
    if best <= 0 or k <= 0:
        return 0.0
    dcg = sum(
        _gain(grade) / math.log2(index + 2)
        for index, grade in enumerate(gains[:k])
        if grade > 0
    )
    ideal = _gain(best)
    return min(1.0, dcg / ideal) if ideal > 0 else 0.0


def _row_rank(gains: list[int], k: int) -> int:
    return first_hit_rank([grade > 0 for grade in gains], k)


def per_query(row: dict[str, Any], k: int) -> dict[str, float]:
    """The three per-query values a comparison is built from."""
    gains = [int(grade) for grade in row.get("gains", [])]
    rank = _row_rank(gains, k)
    return {
        "recall": 1.0 if rank > 0 else 0.0,
        "mrr": reciprocal_rank(rank),
        "ndcg": graded_ndcg(gains, int(row.get("best", 1)), k),
    }


def summarize(per_query_rows: list[dict[str, Any]], k: int = 5) -> dict[str, Any]:
    """Aggregate per-query rows into recall/MRR/nDCG plus empty and duplicate rates."""
    total = len(per_query_rows)
    if total == 0:
        return {
            "queries": 0,
            "recall_at_k": 0.0,
            "mrr_at_k": 0.0,
            "ndcg_at_k": 0.0,
            "empty_rate": 0.0,
            "duplicate_rate": 0.0,
        }
    values = [per_query(row, k) for row in per_query_rows]
    return {
        "queries": total,
        "recall_at_k": sum(value["recall"] for value in values) / total,
        "mrr_at_k": sum(value["mrr"] for value in values) / total,
        "ndcg_at_k": sum(value["ndcg"] for value in values) / total,
        "empty_rate": sum(1 for row in per_query_rows if row.get("count", 0) == 0)
        / total,
        "duplicate_rate": sum(int(row.get("duplicates", 0)) for row in per_query_rows)
        / max(1, sum(int(row.get("count", 0)) for row in per_query_rows)),
    }


def metric_series(
    per_query_rows: list[dict[str, Any]], k: int
) -> dict[str, list[float]]:
    """Per-query metric values, aligned by query order for paired resampling."""
    values = [per_query(row, k) for row in per_query_rows]
    return {
        name: [value[name] for value in values] for name in ("recall", "mrr", "ndcg")
    }


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if not ordered:
        return 0.0
    return (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2.0
    )


def _percentile(ordered: list[float], share: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(share * (len(ordered) - 1))))
    return ordered[index]


def bootstrap_ci(
    values: list[float],
    *,
    iters: int = BOOTSTRAP_ITERS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, float]:
    """Mean with a 95% resampling interval and the smallest difference it can resolve.

    The interval is what makes a knob comparison honest: a 0.02 move inside it is
    noise, and reporting it as an improvement is how a benchmark starts lying.
    """
    if not values:
        return {"mean": 0.0, "lo": 0.0, "hi": 0.0, "mdd": 0.0}
    rng = random.Random(seed)
    size = len(values)
    means: list[float] = []
    for _ in range(iters):
        means.append(sum(values[rng.randrange(size)] for _ in range(size)) / size)
    means.sort()
    mean = sum(values) / size
    lo, hi = _percentile(means, 0.025), _percentile(means, 0.975)
    variance = sum((value - mean) ** 2 for value in values) / max(1, size - 1)
    return {
        "mean": mean,
        "lo": lo,
        "hi": hi,
        "mdd": 1.96 * math.sqrt(variance / size),
    }


def paired_delta(
    variant: list[float],
    baseline: list[float],
    *,
    iters: int = BOOTSTRAP_ITERS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Difference against a baseline on the same queries, with its interval.

    Resampling whole queries (not single values) keeps the pairing: both methods
    answered the same question set, so the difference is measured per question.
    """
    if len(variant) != len(baseline):
        raise ValueError("paired comparison needs the same query set")
    if not variant:
        return {"delta": 0.0, "lo": 0.0, "hi": 0.0, "significant": False}
    diffs = [a - b for a, b in zip(variant, baseline, strict=True)]
    rng = random.Random(seed)
    size = len(diffs)
    means: list[float] = []
    for _ in range(iters):
        means.append(sum(diffs[rng.randrange(size)] for _ in range(size)) / size)
    means.sort()
    lo, hi = _percentile(means, 0.025), _percentile(means, 0.975)
    return {
        "delta": sum(diffs) / size,
        "lo": lo,
        "hi": hi,
        "significant": lo > 0.0 or hi < 0.0,
    }


def distribution(values: list[float]) -> dict[str, float]:
    """Min/median/max of a score list, for spotting ranking-scale drift."""
    if not values:
        return {"min": 0.0, "median": 0.0, "max": 0.0}
    ordered = sorted(values)
    return {"min": ordered[0], "median": _median(ordered), "max": ordered[-1]}
