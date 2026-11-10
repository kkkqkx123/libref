"""Mini-repo end to end: from indexing to closed patterns persisted in the DB."""

from __future__ import annotations

import json

import pytest

from libref.services.code.code_ingest import index_repo
from libref.services.code.mining_refresh import mine_patterns
from tests.conftest import copy_mini_repo

pytestmark = pytest.mark.integration


def test_ingest_repo_end_to_end(conn, tmp_path, monkeypatch):
    repo = copy_mini_repo(tmp_path)
    repos_dir = tmp_path / "repos"
    counts = index_repo(
        conn,
        "demo",
        "0.1",
        str(repo),
        repos_dir=repos_dir,
    )
    assert counts["files"] == 5
    assert counts["parsed"] == 5
    assert counts["source_chunks"] > 0
    repo_docs = conn.execute(
        "SELECT count(*) FROM documents WHERE url LIKE 'repo://%'"
    ).fetchone()[0]
    assert repo_docs == counts["source_chunks"]
    calls = conn.execute(
        "SELECT callee_name, confidence, caller_file, caller FROM call_instances"
    ).fetchall()
    assert calls
    assert any(c[0] == "numpy.array" and c[1] == "INFERRED" for c in calls)
    assert any(c[0] == "d.helper" and c[1] == "EXTRACTED" for c in calls)
    assert any(c[1] == "AMBIGUOUS" for c in calls)
    pending = conn.execute(
        "SELECT count(*) FROM review_status WHERE entity_type='call_instance'"
        " AND status='pending'"
    ).fetchone()[0]
    assert pending >= 1
    results = mine_patterns(conn, "demo", "0.1", minsup=2)
    assert ["numpy.array", "numpy.zeros"] in [r["pattern"] for r in results]
    stored = conn.execute(
        "SELECT pattern, support, is_closed FROM seq_patterns"
    ).fetchall()
    assert stored
    for raw, support, closed in stored:
        assert closed == 1 and support >= 2
        assert isinstance(json.loads(raw), list)
    second = index_repo(conn, "demo", "0.1", str(repo), repos_dir=repos_dir)
    assert second["cached"] == 5 and second["parsed"] == 0
    assert second["source_chunks"] == counts["source_chunks"]
    n_calls = conn.execute("SELECT count(*) FROM call_instances").fetchone()[0]
    assert n_calls == len(calls)
    commit = conn.execute("SELECT commit_sha FROM call_instances LIMIT 1").fetchone()[0]
    assert commit == "local"
    try:
        index_repo(conn, "demo", "0.1", str(repo) + "_other", repos_dir=repos_dir)
    except ValueError:
        pass
    else:
        raise AssertionError("different source for same version must be rejected")
