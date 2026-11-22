"""Integration checks for facet persistence, health signals, labels, and drift."""

from __future__ import annotations

import pytest

from libref.services.code.code_ingest import index_repo
from libref.services.code.mining_read import MiningReadService
from libref.services.code.mining_refresh import induce_labels, mine_patterns
from tests.conftest import copy_mini_repo, make_settings

pytestmark = pytest.mark.integration


def _seed_demo(conn, tmp_path):
    dst = copy_mini_repo(tmp_path)
    index_repo(conn, "demo", "0.1", str(dst), repos_dir=tmp_path / "repos")
    mine_patterns(conn, "demo", "0.1", minsup=2, profile="implementation")
    return MiningReadService(conn)


def test_facets_persisted_and_deterministic(conn, tmp_path):
    svc = _seed_demo(conn, tmp_path)
    first = svc.list_facets("demo", "0.1", profile="implementation", limit=100000)
    assert first
    assert any(
        r["facet"] == "arg_shape" and r["value"].startswith("numpy.array(")
        for r in first
    )
    mine_patterns(conn, "demo", "0.1", minsup=2, profile="implementation")
    second = svc.list_facets("demo", "0.1", profile="implementation", limit=100000)
    assert first == second
    filtered = svc.list_facets(
        "demo", "0.1", facet="call_shape", profile="implementation"
    )
    assert filtered and all(r["facet"] == "call_shape" for r in filtered)
    by_api = svc.list_facets("demo", "0.1", api="numpy.array", profile="implementation")
    assert by_api and all("numpy.array" in r["value"] for r in by_api)


def test_empty_mining_clears_facets(conn, tmp_path):
    _seed_demo(conn, tmp_path)
    svc = MiningReadService(conn)
    assert svc.list_facets("demo", "0.1", profile="implementation")
    mine_patterns(conn, "demo", "0.1", min_args=10**9, profile="implementation")
    assert svc.list_facets("demo", "0.1", profile="implementation") == []
    assert svc.list_patterns("demo", "0.1", profile="implementation") == []


def test_health_carries_facet_counts_and_reuse(conn, tmp_path):
    svc = _seed_demo(conn, tmp_path)
    health = svc.mining_health("demo", "0.1", profile="implementation")
    assert health["reusability"] >= 0.0
    assert health["diversity"] >= 0.0
    assert health["facet_counts"]
    assert health["facet_counts"].get("arg_shape", 0) >= 1


def test_feature_summary_and_facet_metrics(conn, tmp_path):
    svc = _seed_demo(conn, tmp_path)
    summary = svc.facet_summary("demo", "0.1", profile="implementation")
    assert len(summary) == 1
    assert summary[0]["facets"]
    assert summary[0]["patterns"] >= 1
    rows = svc.list_facets("demo", "0.1", facet="arg_shape", profile="implementation")
    metrics = svc.facet_metrics(
        "arg_shape", rows[0]["value"], "demo", "0.1", profile="implementation"
    )
    assert metrics["calls"] == rows[0]["calls"]
    assert 0.0 <= metrics["coverage"] <= 1.0


def test_drift_self_compare_is_empty(conn, tmp_path):
    svc = _seed_demo(conn, tmp_path)
    assert (
        svc.profile_drift(
            "demo",
            "0.1",
            profile="implementation",
            compare_profile="implementation",
        )
        == []
    )
    with pytest.raises(ValueError):
        svc.profile_drift("demo", "0.1", profile="implementation")


def test_cluster_labels_roundtrip(conn, tmp_path, monkeypatch):
    svc = _seed_demo(conn, tmp_path)
    assert svc.list_cluster_labels("demo", "0.1", profile="implementation") == []

    async def fake_chat(messages):
        _ = messages
        return (
            '{"name": "array group", "summary": "build then fill", '
            '"category_path": ["array", "create"], "workflow": "array then zeros", '
            '"related_apis": [], '
            '"source_note": "Observed in implementation sample, not an official recommendation."}'
        )

    import asyncio

    settings = make_settings(
        monkeypatch, tmp_path, induction_enabled=True, induction_topn=5
    )
    out = asyncio.run(
        induce_labels(
            conn, settings, "demo", "0.1", profile="implementation", chat=fake_chat
        )
    )
    clusters = out["clusters"]
    assert clusters["labeled"] >= 1
    assert clusters["failed"] == 0
    labels = svc.list_cluster_labels("demo", "0.1", profile="implementation")
    assert labels
    assert labels[0]["source_note"]
    assert any("numpy.array" in m for m in labels[0]["members"])
    again = asyncio.run(
        induce_labels(
            conn, settings, "demo", "0.1", profile="implementation", chat=fake_chat
        )
    )
    assert again["clusters"]["skipped"] >= 1


def test_api_args_view_carries_facet_tags(conn, tmp_path):
    svc = _seed_demo(conn, tmp_path)
    rows = svc.api_insight("args", api="numpy.array", library="demo", version="0.1")
    assert rows
    assert all(str(r["facet"]).startswith("numpy.array(") for r in rows)
    assert any(r.get("facet_calls", 0) > 0 for r in rows)
