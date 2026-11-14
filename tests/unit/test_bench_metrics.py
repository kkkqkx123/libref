"""Benchmark metric math: golden rankings over the pure helpers."""

from __future__ import annotations

import pytest

from tests.e2e.bench.metrics import (
    bootstrap_ci,
    distribution,
    first_hit_rank,
    graded_ndcg,
    paired_delta,
    per_query,
    reciprocal_rank,
    summarize,
)

pytestmark = pytest.mark.unit


def test_first_hit_rank_stops_at_top_k() -> None:
    assert first_hit_rank([False, True, True], 5) == 2
    assert first_hit_rank([False, False, True], 2) == 0
    assert first_hit_rank([], 5) == 0


def test_reciprocal_rank_degrades_with_rank() -> None:
    assert reciprocal_rank(0) == 0.0
    assert reciprocal_rank(4) == 0.25


def test_graded_ndcg_prefers_the_named_section_over_the_page() -> None:
    """A section hit at rank 2 must beat a page-only hit at the same rank."""
    assert graded_ndcg([2], best=2, k=5) == 1.0
    assert graded_ndcg([1], best=2, k=5) == pytest.approx(1 / 3)
    assert graded_ndcg([1], best=1, k=5) == pytest.approx(1.0)
    assert graded_ndcg([0], best=2, k=5) == 0.0
    assert graded_ndcg([2], best=2, k=0) == 0.0


def test_per_query_reads_rank_and_gain_from_grades() -> None:
    row = {"gains": [0, 1, 2], "best": 2}
    value = per_query(row, k=5)
    assert value["recall"] == 1.0
    assert value["mrr"] == pytest.approx(0.5)
    assert value["ndcg"] == pytest.approx((1 / 1.584962500721156 + 3 / 2.0) / 3)


def test_summarize_averages_and_reports_rates() -> None:
    rows = [
        {"gains": [2], "best": 2, "count": 3, "duplicates": 0},
        {"gains": [], "best": 1, "count": 0, "duplicates": 0},
        {"gains": [0, 1], "best": 2, "count": 4, "duplicates": 1},
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


def test_bootstrap_interval_brackets_the_mean_and_shrinks_with_n() -> None:
    tight = bootstrap_ci([1.0] * 10)
    assert tight["mean"] == 1.0
    assert tight["lo"] == 1.0 and tight["hi"] == 1.0

    noisy = bootstrap_ci([0.0, 1.0] * 40)
    assert noisy["lo"] < noisy["mean"] < noisy["hi"]
    assert noisy["mdd"] < 0.2


def test_paired_delta_reports_significance_only_outside_the_interval() -> None:
    up = paired_delta([1.0] * 30, [0.0] * 30)
    assert up["delta"] == 1.0
    assert up["significant"] is True

    flat = paired_delta([1.0, 0.0] * 15, [1.0, 0.0] * 15)
    assert flat["delta"] == 0.0
    assert flat["significant"] is False


def test_paired_delta_rejects_unpaired_inputs() -> None:
    with pytest.raises(ValueError):
        paired_delta([1.0], [])
