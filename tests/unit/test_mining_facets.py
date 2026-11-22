"""Unit checks for usage facet aggregation, selection, guards, and metrics."""

from __future__ import annotations

import pytest

from libref.mining.facets import (
    FACET_ARG_SHAPE,
    FACET_CALL_ORDER,
    FACET_CALL_SHAPE,
    FACET_COMBO,
    aggregate_facets,
    facet_callee,
    mask_call_shape,
    order_bucket,
    score_facet_row,
    select_facets,
    split_shape_value,
)

pytestmark = pytest.mark.unit


def _row(callee, args=(), relation="direct", scope="s", line=1, order=0):
    return {
        "callee": callee,
        "args": list(args),
        "relation": relation,
        "scope": scope,
        "line": line,
        "order": order,
    }


def test_mask_call_shape_masks_values_but_keeps_keywords():
    assert mask_call_shape("pkg.f", ["a=1", "'x'", "flag=True"]) == (
        "pkg.f(a=0, '...', flag=True)"
    )
    assert mask_call_shape("pkg.f", []) == "pkg.f()"


def test_facet_callee_across_encodings():
    assert facet_callee("pkg.f(a=0)") == "pkg.f"
    assert facet_callee("pkg.f@direct") == "pkg.f"
    assert facet_callee("pkg.f(a=0)@direct") == "pkg.f"
    assert facet_callee("pkg.f") == "pkg.f"


def test_order_bucket_thirds():
    assert [order_bucket(r, 3) for r in range(3)] == ["first", "middle", "last"]
    assert order_bucket(0, 1) == "first"


def test_golden_facet_distribution():
    rows = [
        _row("a.f", ["x=1"], scope="s1", line=1, order=0),
        _row("a.f", ["x=2"], scope="s1", line=2, order=1),
        _row("a.f", ["x=3"], scope="s2", line=1, order=0),
        _row("a.g", [], scope="s1", line=3, order=2),
    ]
    out = {
        (r["facet"], r["value"]): (r["calls"], r["scopes"])
        for r in aggregate_facets(rows)
    }
    assert out[(FACET_ARG_SHAPE, "a.f(x=0)")] == (3, 2)
    assert out[(FACET_ARG_SHAPE, "a.g()")] == (1, 1)
    assert out[(FACET_CALL_SHAPE, "a.f@direct")] == (3, 2)
    assert out[(FACET_CALL_ORDER, "a.f@first")] == (2, 2)
    assert out[(FACET_CALL_ORDER, "a.g@last")] == (1, 1)
    assert out[(FACET_COMBO, "a.f(x=0)@direct")] == (3, 2)


def test_aggregate_is_deterministic_and_pruned():
    rows = [_row(f"m.f{i % 4}", [f"v={i}"], scope=f"s{i % 3}") for i in range(40)]
    first = aggregate_facets(rows, topn=2)
    second = aggregate_facets(rows, topn=2)
    assert first == second
    by_facet: dict[str, int] = {}
    for row in first:
        by_facet[row["facet"]] = by_facet.get(row["facet"], 0) + 1
    assert all(count <= 2 for count in by_facet.values())


def test_min_gain_drops_long_tail():
    rows = [_row("a.f", scope="s1")] * 9 + [_row("a.g", scope="s2")]
    out = aggregate_facets(rows, min_gain=0.2)
    values = {(r["facet"], r["value"]) for r in out}
    assert (FACET_ARG_SHAPE, "a.f()") in values
    assert (FACET_ARG_SHAPE, "a.g()") not in values


def test_guards_stay_silent_without_signal():
    rows = [_row("a.f", scope="s1", line=1), _row("b.g", scope="s1", line=2)]
    assert aggregate_facets(rows, guard_enabled=False) == aggregate_facets(rows)
    out = aggregate_facets(rows, guard_enabled=True)
    assert all(r["facet"] != "guard" for r in out)


def test_guard_pairs_and_checks():
    rows = [
        _row("db.open", scope="s1", line=1, order=0),
        _row("db.close", scope="s1", line=5, order=1),
        _row("db.check_conn", scope="s2", line=1, order=0),
        _row("db.query", scope="s2", line=2, order=1),
    ]
    out = aggregate_facets(rows, guard_enabled=True)
    guards = {r["value"] for r in out if r["facet"] == "guard"}
    assert "db.open@paired:db.close" in guards
    assert "db.close@paired:db.open" in guards
    assert "db.query@checked:db.check_conn" in guards


def test_select_facets_caps_candidates():
    rows = [
        {"facet": FACET_ARG_SHAPE, "value": f"a.f{i}", "calls": 10 - i, "scopes": 1}
        for i in range(6)
    ]
    out = select_facets(rows, topn=3)
    assert [r["value"] for r in out] == ["a.f0", "a.f1", "a.f2"]


def test_score_facet_row_reports_coverage_and_lift():
    peers = [
        {"facet": FACET_ARG_SHAPE, "value": "a.f(x=0)", "calls": 6, "scopes": 3},
        {"facet": FACET_ARG_SHAPE, "value": "a.f(y=0)", "calls": 1, "scopes": 1},
        {"facet": FACET_ARG_SHAPE, "value": "b.g(z=0)", "calls": 3, "scopes": 2},
    ]
    scored = score_facet_row(
        FACET_ARG_SHAPE,
        "a.f(x=0)",
        6,
        3,
        peer_rows=peers,
        total_calls=10,
        total_scopes=5,
    )
    assert scored["coverage"] == 0.6
    assert scored["concentration"] == 0.6
    assert scored["lift"] > 1.0


def test_split_shape_value():
    assert split_shape_value("a.f(x=0)") == ("a.f", "(x=0)")
    assert split_shape_value("a.f") == ("a.f", "")
