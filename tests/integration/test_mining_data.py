"""Relation-mining data shapes: golden samples for order preservation, denoising, sweeping, pruning, and retrieval queries."""

from __future__ import annotations

import json

import pytest

from libref.services.code_ingest import index_repo
from libref.services.mining_refresh import mine_patterns
from libref.services.libraries import LibraryService
from libref.services.mining_read import MiningReadService
from libref.services.search import SearchService
from tests.conftest import copy_mini_repo

pytestmark = pytest.mark.integration


def _seed_calls(conn, library="demo", version="t", rows=()):
    library_id, version_id = LibraryService(conn).ensure_library_version(library, version)
    for i, (callee, path, caller, line, order, start, conf, args) in enumerate(rows):
        conn.execute(
            "INSERT INTO call_instances(call_uid, callee_name, args, repo,"
            " caller_file, line_start, line_end, confidence,"
            " library_id, version_id, caller, call_order, start_byte)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"uid-{i}", callee, json.dumps(args), "r", path,
             line, line, conf, library_id, version_id, caller, order, start),
        )
    conn.commit()
    return library_id, version_id


def test_sequence_order_uses_byte_offset_not_just_line(conn):
    _seed_calls(conn, rows=[
        ("second", "a.py", "f", 10, 2, 200, "INFERRED", []),
        ("first", "a.py", "f", 10, 1, 100, "INFERRED", []),
        ("second", "b.py", "g", 10, 2, 200, "INFERRED", []),
        ("first", "b.py", "g", 10, 1, 100, "INFERRED", []),
    ])
    results = mine_patterns(conn, "demo", "t", minsup=2)
    assert [r["pattern"] for r in results] == [["first", "second"]]


def test_ambiguous_excluded_by_default(conn):
    _seed_calls(conn, rows=[
        ("real.a", "a.py", "f", 1, 1, 10, "INFERRED", []),
        ("mystery", "a.py", "f", 2, 2, 20, "AMBIGUOUS", []),
        ("real.a", "b.py", "g", 1, 1, 10, "INFERRED", []),
        ("mystery", "b.py", "g", 2, 2, 20, "AMBIGUOUS", []),
    ])
    assert mine_patterns(conn, "demo", "t", minsup=2) == []
    results = mine_patterns(conn, "demo", "t", minsup=2, include_ambiguous=True)
    assert ["real.a", "mystery"] in [r["pattern"] for r in results]


def test_stale_patterns_swept(conn):
    _seed_calls(conn, rows=[
        ("a.one", "a.py", "f", 1, 1, 10, "INFERRED", []),
        ("a.two", "a.py", "f", 2, 2, 20, "INFERRED", []),
        ("a.one", "b.py", "g", 1, 1, 10, "INFERRED", []),
        ("a.two", "b.py", "g", 2, 2, 20, "INFERRED", []),
    ])
    assert mine_patterns(conn, "demo", "t", minsup=2) != []
    conn.execute("DELETE FROM call_instances WHERE callee_name='a.two'")
    conn.commit()
    assert mine_patterns(conn, "demo", "t", minsup=2) == []
    assert conn.execute("SELECT count(*) FROM seq_patterns").fetchone()[0] == 0


def test_topn_keeps_exact_frequent_names(conn):
    rows = []
    for f in ("a.py", "b.py"):
        rows.append(("aa.bb.cc.dd", f, "f", 1, 1, 10, "INFERRED", []))
        rows.append(("aa.bb.cc.ee", f, "f", 2, 2, 20, "INFERRED", []))
    rows.append(("rare.z", "a.py", "f", 3, 3, 30, "INFERRED", []))
    _seed_calls(conn, rows=rows)
    results = mine_patterns(conn, "demo", "t", minsup=2, topn=2)
    assert [["aa.bb.cc.dd", "aa.bb.cc.ee"]] == [r["pattern"] for r in results]
    assert mine_patterns(conn, "demo", "t", minsup=2, topn=1) == []


def test_retrieval_queries_over_mined_data(conn, tmp_path):
    dst = copy_mini_repo(tmp_path)
    index_repo(conn, "demo", "0.1", str(dst), repos_dir=tmp_path / "repos")
    mine_patterns(conn, "demo", "0.1", minsup=2)
    svc = MiningReadService(conn)

    usage = {u["api"]: u for u in svc.api_usage_counts("demo", "0.1")}
    assert usage["numpy.array"]["calls"] == 4
    assert usage["numpy.array"]["scopes"] == 4

    related = {r["api"]: r for r in svc.related_apis("numpy.array", "demo", "0.1")}
    assert related["numpy.zeros"]["scopes"] == 4

    shapes = svc.arg_shapes("numpy.zeros", "demo", "0.1")
    assert shapes and shapes[0]["count"] >= 1

    patterns = svc.list_patterns("demo", "0.1", containing=["numpy.array"])
    assert ["numpy.array", "numpy.zeros"] in [p["pattern"] for p in patterns]

    evidence = svc.pattern_evidence(["numpy.array", "numpy.zeros"], "demo", "0.1")
    assert len(evidence) >= 2
    first = evidence[0]
    assert first["caller_file"] and first["caller"]
    assert [c["callee"] for c in first["calls"]][:2] == ["numpy.array", "numpy.zeros"]
    assert all(c["confidence"] != "AMBIGUOUS" for c in first["calls"])


def test_minlen_filters_short_patterns(conn):
    _seed_calls(conn, rows=[
        ("p.a", "a.py", "f", 1, 1, 10, "INFERRED", []),
        ("p.b", "a.py", "f", 2, 2, 20, "INFERRED", []),
        ("p.c", "a.py", "f", 3, 3, 30, "INFERRED", []),
        ("p.a", "b.py", "g", 1, 1, 10, "INFERRED", []),
        ("p.b", "b.py", "g", 2, 2, 20, "INFERRED", []),
        ("p.c", "b.py", "g", 3, 3, 30, "INFERRED", []),
        ("p.a", "c.py", "h", 1, 1, 10, "INFERRED", []),
        ("p.b", "c.py", "h", 2, 2, 20, "INFERRED", []),
    ])
    short = mine_patterns(conn, "demo", "t", minsup=2, minlen=2)
    assert any(len(r["pattern"]) == 2 for r in short)
    long_only = mine_patterns(conn, "demo", "t", minsup=2, minlen=3)
    assert long_only and all(len(r["pattern"]) >= 3 for r in long_only)
