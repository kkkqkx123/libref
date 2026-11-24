"""Relation-mining data shapes: golden samples for order preservation, denoising, sweeping, pruning, and retrieval queries."""

from __future__ import annotations

import json

import pytest

from libref.services.code.code_ingest import index_repo
from libref.services.code.mining_read import MiningReadService
from libref.services.code.mining_refresh import mine_patterns
from libref.services.registry.libraries import LibraryService
from tests.conftest import copy_mini_repo, index_symbol_repo

pytestmark = pytest.mark.integration


def _seed_calls(conn, library="demo", version="t", rows=()):
    library_id, version_id = LibraryService(conn).ensure_library_version(
        library, version
    )
    from libref.services.code.usage_sources import register_source

    existing = conn.execute(
        "SELECT id FROM code_sources WHERE version_id=? AND source_role='implementation'"
        " ORDER BY id LIMIT 1",
        (version_id,),
    ).fetchone()
    source_id = (
        int(existing[0])
        if existing is not None
        else register_source(
            conn,
            version_id=version_id,
            source_key="seeded:implementation",
            source_role="implementation",
        )
    )
    for i, (callee, path, caller, line, order, start, conf, args) in enumerate(rows):
        scope = f"{path}\0{caller or ''}"
        conn.execute(
            "INSERT INTO call_instances(call_uid, callee_name, args, repo,"
            " caller_file, line_start, line_end, confidence,"
            " library_id, version_id, caller, call_order, start_byte, scope,"
            " source_id, sample_id, source_role, in_target_api)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"uid-{i}",
                callee,
                json.dumps(args),
                "r",
                path,
                line,
                line,
                conf,
                library_id,
                version_id,
                caller,
                order,
                start,
                scope,
                source_id,
                f"{path}::{caller}" if caller else path,
                "implementation",
                1,
            ),
        )
    conn.commit()
    return library_id, version_id


def test_sequence_order_uses_byte_offset_not_just_line(conn, monkeypatch, tmp_path):
    _seed_calls(
        conn,
        rows=[
            ("second", "a.py", "f", 10, 2, 200, "INFERRED", []),
            ("first", "a.py", "f", 10, 1, 100, "INFERRED", []),
            ("second", "b.py", "g", 10, 2, 200, "INFERRED", []),
            ("first", "b.py", "g", 10, 1, 100, "INFERRED", []),
        ],
    )
    results = mine_patterns(conn, "demo", "t", minsup=2, profile="implementation")
    assert [r["pattern"] for r in results] == [["first", "second"]]


def test_ambiguous_excluded_by_default(conn, monkeypatch, tmp_path):
    _seed_calls(
        conn,
        rows=[
            ("real.a", "a.py", "f", 1, 1, 10, "INFERRED", []),
            ("mystery", "a.py", "f", 2, 2, 20, "AMBIGUOUS", []),
            ("real.a", "b.py", "g", 1, 1, 10, "INFERRED", []),
            ("mystery", "b.py", "g", 2, 2, 20, "AMBIGUOUS", []),
        ],
    )
    assert mine_patterns(conn, "demo", "t", minsup=2, profile="implementation") == []
    results = mine_patterns(
        conn,
        "demo",
        "t",
        minsup=2,
        include_ambiguous=True,
        profile="implementation",
    )
    assert ["real.a", "mystery"] in [r["pattern"] for r in results]


def test_stale_patterns_swept(conn, monkeypatch, tmp_path):
    _seed_calls(
        conn,
        rows=[
            ("a.one", "a.py", "f", 1, 1, 10, "INFERRED", []),
            ("a.two", "a.py", "f", 2, 2, 20, "INFERRED", []),
            ("a.one", "b.py", "g", 1, 1, 10, "INFERRED", []),
            ("a.two", "b.py", "g", 2, 2, 20, "INFERRED", []),
        ],
    )
    assert mine_patterns(conn, "demo", "t", minsup=2, profile="implementation") != []
    conn.execute("DELETE FROM call_instances WHERE callee_name='a.two'")
    conn.commit()
    assert mine_patterns(conn, "demo", "t", minsup=2, profile="implementation") == []
    assert conn.execute("SELECT count(*) FROM seq_patterns").fetchone()[0] == 0


def test_topn_keeps_exact_frequent_names(conn, monkeypatch, tmp_path):
    rows = []
    for f in ("a.py", "b.py"):
        rows.append(("aa.bb.cc.dd", f, "f", 1, 1, 10, "INFERRED", []))
        rows.append(("aa.bb.cc.ee", f, "f", 2, 2, 20, "INFERRED", []))
    rows.append(("rare.z", "a.py", "f", 3, 3, 30, "INFERRED", []))
    _seed_calls(conn, rows=rows)
    results = mine_patterns(
        conn, "demo", "t", minsup=2, topn=2, profile="implementation"
    )
    assert [["aa.bb.cc.dd", "aa.bb.cc.ee"]] == [r["pattern"] for r in results]
    assert (
        mine_patterns(conn, "demo", "t", minsup=2, topn=1, profile="implementation")
        == []
    )


def test_retrieval_queries_over_mined_data(conn, tmp_path, monkeypatch):
    dst = copy_mini_repo(tmp_path)
    index_repo(conn, "demo", "0.1", str(dst), repos_dir=tmp_path / "repos")
    mine_patterns(conn, "demo", "0.1", minsup=2, profile="implementation")
    svc = MiningReadService(conn)

    usage = {u["api"]: u for u in svc.api_usage_counts("demo", "0.1")}
    assert usage["numpy.array"]["calls"] == 4
    assert usage["numpy.array"]["scopes"] == 4

    related = {r["api"]: r for r in svc.related_apis("numpy.array", "demo", "0.1")}
    assert related["numpy.zeros"]["scopes"] == 4

    shapes = svc.arg_shapes("numpy.zeros", "demo", "0.1")
    assert shapes and shapes[0]["count"] >= 1

    patterns = svc.list_patterns(
        "demo", "0.1", containing=["numpy.array"], profile="implementation"
    )
    assert ["numpy.array", "numpy.zeros"] in [p["pattern"] for p in patterns]

    raw = svc.pattern_evidence(
        ["numpy.array", "numpy.zeros"],
        "demo",
        "0.1",
        dedup=False,
        per_file_limit=10,
        profile="implementation",
    )
    assert len(raw) >= 2
    diversified = svc.pattern_evidence(
        ["numpy.array", "numpy.zeros"],
        "demo",
        "0.1",
        profile="implementation",
    )
    assert len(diversified) >= 1
    assert len(diversified) <= len(raw)
    evidence = diversified
    first = evidence[0]
    assert first["caller_file"] and first["caller"]
    assert [c["callee"] for c in first["calls"]][:2] == ["numpy.array", "numpy.zeros"]
    assert all(c["confidence"] != "AMBIGUOUS" for c in first["calls"])


def test_minlen_filters_short_patterns(conn, monkeypatch, tmp_path):
    _seed_calls(
        conn,
        rows=[
            ("p.a", "a.py", "f", 1, 1, 10, "INFERRED", []),
            ("p.b", "a.py", "f", 2, 2, 20, "INFERRED", []),
            ("p.c", "a.py", "f", 3, 3, 30, "INFERRED", []),
            ("p.a", "b.py", "g", 1, 1, 10, "INFERRED", []),
            ("p.b", "b.py", "g", 2, 2, 20, "INFERRED", []),
            ("p.c", "b.py", "g", 3, 3, 30, "INFERRED", []),
            ("p.a", "c.py", "h", 1, 1, 10, "INFERRED", []),
            ("p.b", "c.py", "h", 2, 2, 20, "INFERRED", []),
        ],
    )
    short = mine_patterns(
        conn, "demo", "t", minsup=2, minlen=2, profile="implementation"
    )
    assert any(len(r["pattern"]) == 2 for r in short)
    long_only = mine_patterns(
        conn, "demo", "t", minsup=2, minlen=3, profile="implementation"
    )
    assert long_only and all(len(r["pattern"]) >= 3 for r in long_only)


def test_file_scope_calls_form_sequences(conn, monkeypatch, tmp_path):
    _seed_calls(
        conn,
        library="demo",
        version="noscope",
        rows=[
            ("s.a", "a.py", None, 1, 1, 10, "INFERRED", []),
            ("s.b", "a.py", None, 2, 2, 20, "INFERRED", []),
            ("s.a", "b.py", None, 1, 1, 10, "INFERRED", []),
            ("s.b", "b.py", None, 2, 2, 20, "INFERRED", []),
        ],
    )
    results = mine_patterns(conn, "demo", "noscope", minsup=2, profile="implementation")
    assert ["s.a", "s.b"] in [r["pattern"] for r in results]


def test_import_edges_queryable(conn, tmp_path):
    index_symbol_repo(conn, tmp_path)
    svc = MiningReadService(conn)
    by_file = svc.imports_of_file("pkg/app.py", "demo", "0.1")
    assert by_file
    assert any(r["target"] == "pkg.core" for r in by_file)
    assert any(r["target"] == "pkg.factory.make_service" for r in by_file)
    importers = svc.importers_of("pkg.core", "demo", "0.1")
    assert any(r["importer_file"] == "pkg/app.py" for r in importers)


def test_evidence_carries_signatures(conn, tmp_path):
    index_symbol_repo(conn, tmp_path)
    _seed_calls(
        conn,
        version="0.1",
        rows=[
            ("pkg.core.Service", "extra.py", "main2", 1, 1, 10, "INFERRED", []),
            (
                "pkg.core.Service.create",
                "extra.py",
                "main2",
                2,
                2,
                20,
                "INFERRED",
                [],
            ),
        ],
    )
    mine_patterns(conn, "demo", "0.1", minsup=2, minlen=2, profile="implementation")
    svc = MiningReadService(conn)
    patterns = svc.list_patterns("demo", "0.1", limit=10, profile="implementation")
    assert patterns
    target = None
    for entry in patterns:
        names = list(entry.get("pattern") or [])
        if "pkg.core.Service.run" in names or "pkg.core.Service.create" in names:
            target = names
            break
    assert target is not None
    groups = svc.pattern_evidence(
        target, "demo", "0.1", limit=5, profile="implementation"
    )
    assert groups
    assert any(
        str(c.get("callee_signature") or "").strip()
        for g in groups
        for c in (g.get("calls") or [])
    )
    assert any("<call" in str(g.get("rendered") or "") for g in groups)


def test_mine_reads_calls_once(conn):
    _seed_calls(
        conn,
        rows=[
            ("first", "a.py", "f", 1, 1, 10, "INFERRED", []),
            ("second", "a.py", "f", 2, 2, 20, "INFERRED", []),
            ("first", "b.py", "g", 1, 1, 10, "INFERRED", []),
            ("second", "b.py", "g", 2, 2, 20, "INFERRED", []),
        ],
    )
    captured: list[str] = []
    conn.set_trace_callback(captured.append)
    try:
        results = mine_patterns(conn, "demo", "t", minsup=2, profile="implementation")
    finally:
        conn.set_trace_callback(None)
    assert results
    scans = sum(
        1
        for sql in captured
        if sql.startswith("SELECT callee_name, caller_file, caller, confidence")
    )
    assert scans == 1


def test_batch_siblings_match_single_reads(conn):
    _seed_calls(
        conn,
        rows=[
            ("first", "a.py", "f", 1, 1, 10, "INFERRED", []),
            ("neighbor", "a.py", "f", 2, 2, 20, "INFERRED", []),
            ("first", "b.py", "g", 1, 1, 10, "INFERRED", []),
            ("neighbor", "b.py", "g", 2, 2, 20, "INFERRED", []),
        ],
    )
    svc = MiningReadService(conn)
    single = svc.related_apis("first", "demo", "t", limit=4)
    captured: list[str] = []
    conn.set_trace_callback(captured.append)
    try:
        batch = svc.related_apis_batch(["first"], "demo", "t", limit=4)
    finally:
        conn.set_trace_callback(None)
    assert batch["first"] == single
    assert sum(1 for sql in captured if sql.startswith("WITH target")) == 1


def test_read_scope_uses_persisted_column(conn):
    _seed_calls(
        conn,
        rows=[("first", "a.py", "f", 1, 1, 10, "INFERRED", [])],
    )
    captured: list[str] = []
    conn.set_trace_callback(captured.append)
    try:
        MiningReadService(conn).api_usage_counts("demo", "t")
    finally:
        conn.set_trace_callback(None)
    sql = " ".join(captured)
    assert "call_instances.scope" in sql
    assert "COALESCE(scope" not in sql
