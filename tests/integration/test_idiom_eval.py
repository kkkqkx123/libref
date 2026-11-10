"""End-to-end checks for idiom snippets, dedup, health, labels, and eval closure."""

from __future__ import annotations

import pytest

from libref.services.code_ingest import index_repo
from libref.services.mining_read import MiningReadService
from libref.services.mining_refresh import induce_labels, mine_patterns
from tests.conftest import copy_mini_repo

pytestmark = pytest.mark.integration


def _seed_demo(conn, tmp_path):
    dst = copy_mini_repo(tmp_path)
    index_repo(conn, "demo", "0.1", str(dst), repos_dir=tmp_path / "repos")
    mine_patterns(conn, "demo", "0.1", minsup=2)
    return MiningReadService(conn)


def test_evidence_snippet_and_per_file_dedup(conn, tmp_path):
    svc = _seed_demo(conn, tmp_path)
    raw = svc.pattern_evidence(
        ["numpy.array", "numpy.zeros"],
        "demo",
        "0.1",
        limit=10,
        per_file_limit=10,
        dedup=False,
    )
    assert len(raw) >= 2
    groups = svc.pattern_evidence(
        ["numpy.array", "numpy.zeros"], "demo", "0.1", limit=10
    )
    assert len(groups) >= 1
    assert len(groups) <= len(raw)
    assert all("snippet" in g for g in groups)
    first = groups[0]["snippet"]
    assert not first["invalid"]
    assert any(not line.get("gap") and line.get("hit") for line in first["lines"])
    limited = svc.pattern_evidence(
        ["numpy.array", "numpy.zeros"],
        "demo",
        "0.1",
        limit=10,
        per_file_limit=1,
        dedup=True,
    )
    files = [g["caller_file"] for g in limited]
    assert len(files) == len(set(files))


def test_intersection_prescreen_matches_precise(conn, tmp_path):
    svc = _seed_demo(conn, tmp_path)
    strict = svc.related_common(["numpy.array", "numpy.zeros"], "demo", "0.1")
    assert strict == []
    relaxed = svc.related_common(
        ["numpy.array", "numpy.zeros"], "demo", "0.1", include_ambiguous=True
    )
    assert any(r["api"] == "print" for r in relaxed)
    patterns = svc.list_patterns(
        "demo", "0.1", containing=["numpy.array", "numpy.zeros"]
    )
    assert any("numpy.zeros" in p["pattern"] for p in patterns)


def test_health_and_labels_roundtrip(conn, tmp_path):
    svc = _seed_demo(conn, tmp_path)
    health = svc.mining_health("demo", "0.1")
    assert health["patterns"] >= 1
    assert health["total_support"] >= health["patterns"]

    async def fake_chat(messages):
        _ = messages
        return (
            '{"name": "array init", "summary": "build then fill", '
            '"category_path": ["array", "create"], "workflow": "array then zeros", '
            '"related_apis": ["numpy.array"]}'
        )

    import asyncio

    from libref.config import load_settings

    settings = load_settings(overrides={"induction_enabled": True, "induction_topn": 5})
    out = asyncio.run(induce_labels(conn, settings, "demo", "0.1", chat=fake_chat))
    assert out["labeled"] >= 1
    assert out["failed"] == 0
    labels = svc.list_labels("demo", "0.1")
    assert labels
    assert labels[0]["name"] == "array init"
    again = asyncio.run(induce_labels(conn, settings, "demo", "0.1", chat=fake_chat))
    assert again["skipped"] >= 1
    assert again["labeled"] == 0


def test_eval_closure_tasks_resolve_and_execute(conn, tmp_path):
    svc = _seed_demo(conn, tmp_path)
    tasks = [
        {
            "intent": "create an array then fill with zeros",
            "apis": ["numpy.array", "numpy.zeros"],
        },
        {"intent": "prepare data with arrays", "apis": ["numpy.array"]},
    ]
    for task in tasks:
        if len(task["apis"]) > 1:
            related = svc.related_common(
                task["apis"], "demo", "0.1", include_ambiguous=True
            )
            assert related
            patterns = svc.list_patterns("demo", "0.1", containing=task["apis"])
        else:
            patterns = svc.list_patterns("demo", "0.1", containing=task["apis"])
        assert patterns
        evidence = svc.pattern_evidence(patterns[0]["pattern"], "demo", "0.1", limit=3)
        assert evidence
        snippet = evidence[0]["snippet"]
        assert not snippet["invalid"]
        for api in task["apis"]:
            assert any(
                api in line.get("text", "")
                for line in snippet["lines"]
                if not line.get("gap")
            )
        compile(
            "\n".join(line["text"] for line in snippet["lines"] if not line.get("gap")),
            "<idiom>",
            "exec",
        )
