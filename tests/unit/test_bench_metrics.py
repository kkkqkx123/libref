"""Benchmark metric math: golden rankings over the pure helpers."""

from __future__ import annotations

import pytest

from tests.e2e.bench.metrics import (
    distribution,
    first_hit_rank,
    ndcg,
    reciprocal_rank,
    summarize,
)

pytestmark = pytest.mark.unit


def test_first_hit_rank_stops_at_top_k() -> None:
    assert first_hit_rank([False, True, True], 5) == 2
    assert first_hit_rank([False, False, True], 2) == 0
    assert first_hit_rank([], 5) == 0


def test_reciprocal_rank_and_ndcg_degrade_with_rank() -> None:
    assert reciprocal_rank(0) == 0.0
    assert reciprocal_rank(4) == 0.25
    assert ndcg(1, 5) == 1.0
    assert ndcg(6, 5) == 0.0
    assert ndcg(2, 5) == pytest.approx(1 / 1.584962500721156)


def test_summarize_averages_and_reports_rates() -> None:
    rows = [
        {"rank": 1, "count": 3, "duplicates": 0},
        {"rank": 0, "count": 0, "duplicates": 0},
        {"rank": 2, "count": 4, "duplicates": 1},
    ]
    out = summarize(rows, k=5)
    assert out["queries"] == 3
    assert out["recall_at_k"] == pytest.approx(2 / 3)
    assert out["mrr_at_k"] == pytest.approx((1 + 0 + 0.5) / 3)
    assert out["empty_rate"] == pytest.approx(1 / 3)
    assert out["duplicate_rate"] == pytest.approx(1 / 7)


def test_summarize_empty_rows_is_zero() -> None:
    out = summarize([], k=5)
    assert out["queries"] == 0
    assert out["recall_at_k"] == 0.0
    assert out["duplicate_rate"] == 0.0


def test_distribution_handles_even_and_empty_inputs() -> None:
    assert distribution([]) == {"min": 0.0, "median": 0.0, "max": 0.0}
    assert distribution([3.0, 1.0, 2.0, 4.0]) == {
        "min": 1.0,
        "median": 2.5,
        "max": 4.0,
    }
