"""Golden samples for hierarchical features and interface grouping, plus end-to-end DB persistence assertions."""

from __future__ import annotations

import json

import pytest

from libref.mining.cluster import cluster_apis
from libref.mining.cooccur import build_scope_sets
from libref.mining.feature_tree import build_feature_tree
from libref.services.code_ingest import index_repo
from libref.services.libraries import LibraryService
from libref.services.mining_read import MiningReadService
from libref.services.mining_refresh import mine_patterns
from tests.conftest import copy_mini_repo

pytestmark = pytest.mark.integration


def test_feature_tree_hand_computed():
    calls = [
        ("a.b", "f1"),
        ("a.b", "f1"),
        ("a.c", "f1"),
        ("a.b", "f2"),
    ]
    nodes = {
        n["path"]: n
        for n in build_feature_tree(calls, {"a.b": "function", "a.c": "function"})
    }
    assert nodes["a"]["calls"] == 4
    assert nodes["a"]["scopes"] == 2
    assert nodes["a.b"]["calls"] == 3
    assert nodes["a.b"]["scopes"] == 2
    assert nodes["a.c"]["calls"] == 1
    assert nodes["a.b"]["kind"] == "function"


def test_cluster_hand_computed():
    sets = [{"a", "b"}, {"a", "b"}, {"a", "c"}, {"b", "c"}]
    groups = cluster_apis(sets, threshold=0.15, min_size=2)
    assert len(groups) == 1
    assert sorted(groups[0]["members"]) == ["a", "b", "c"]
    single = cluster_apis([{"a"}, {"b"}], threshold=0.15, min_size=2)
    assert single == []


def test_scope_sets_share_scope_key_with_ingest():
    rows = [("a", "f.py", "g"), ("b", "f.py", "g"), ("a", "h.py", None)]
    sets = build_scope_sets(rows)
    assert sorted([sorted(s) for s in sets]) == [["a"], ["a", "b"]]


def test_mine_persists_tree_and_clusters(conn, tmp_path):
    dst = copy_mini_repo(tmp_path)
    index_repo(conn, "demo", "0.1", str(dst), repos_dir=tmp_path / "repos")
    mine_patterns(conn, "demo", "0.1", minsup=2)
    svc = MiningReadService(conn)
    tree = {n["path"]: n for n in svc.feature_tree("demo", "0.1")}
    assert tree["numpy"]["calls"] == 8
    assert tree["numpy"]["scopes"] == 4
    assert tree["numpy.array"]["calls"] == 4
    clusters = svc.api_clusters("demo", "0.1")
    assert clusters
    assert any(
        "numpy.array" in g["members"] and "numpy.zeros" in g["members"]
        for g in clusters
    )
    evidence = svc.pattern_evidence(["numpy.array", "numpy.zeros"], "demo", "0.1")
    assert len(evidence) >= 2


def test_mine_clears_derived_when_calls_gone(conn):
    library_id, version_id = LibraryService(conn).ensure_library_version("demo", "t")
    conn.execute(
        "INSERT INTO call_instances(call_uid, callee_name, args, repo,"
        " caller_file, line_start, line_end, confidence,"
        " library_id, version_id, caller, call_order, start_byte)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "uid-1",
            "a.b",
            json.dumps([]),
            "r",
            "f.py",
            1,
            1,
            "INFERRED",
            library_id,
            version_id,
            "g",
            1,
            10,
        ),
    )
    conn.commit()
    mine_patterns(conn, "demo", "t", minsup=1, minlen=1)
    assert conn.execute("SELECT count(*) FROM feature_nodes").fetchone()[0] >= 1
    conn.execute("DELETE FROM call_instances WHERE version_id=?", (version_id,))
    conn.commit()
    assert mine_patterns(conn, "demo", "t", minsup=1, minlen=1) == []
    assert conn.execute("SELECT count(*) FROM feature_nodes").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM api_clusters").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM seq_patterns").fetchone()[0] == 0
