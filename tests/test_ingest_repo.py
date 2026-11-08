"""迷你仓库端到端：建索引到闭合模式落库。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from libref.services.ingest import index_repo, mine_patterns


def _copy_fixture(tmp_path: Path) -> Path:
    src = Path("tests/fixtures/mini_repo")
    dst = tmp_path / "mini_repo"
    shutil.copytree(src, dst)
    return dst


def test_ingest_repo_end_to_end(conn, tmp_path):
    repo = _copy_fixture(tmp_path)
    repos_dir = tmp_path / "repos"
    counts = index_repo(
        conn, "demo", "0.1", str(repo), repos_dir=repos_dir,
    )
    assert counts["files"] == 5
    assert counts["parsed"] == 5
    calls = conn.execute(
        "SELECT callee_name, confidence, caller_file, caller FROM call_instances"
    ).fetchall()
    assert calls
    assert any(c[0] == "numpy.array" and c[1] == "INFERRED" for c in calls)
    assert any(c[0] == "helper" and c[1] == "EXTRACTED" for c in calls)
    assert any(c[1] == "AMBIGUOUS" for c in calls)
    pending = conn.execute(
        "SELECT count(*) FROM review_status WHERE entity_type='call_instance'"
        " AND status='pending'"
    ).fetchone()[0]
    assert pending >= 1
    results = mine_patterns(conn, "demo", "0.1", minsup=2)
    assert ["numpy.array", "numpy.zeros"] in [r["pattern"] for r in results]
    stored = conn.execute("SELECT pattern, support, is_closed FROM seq_patterns").fetchall()
    assert stored
    for raw, support, closed in stored:
        assert closed == 1 and support >= 2
        assert isinstance(json.loads(raw), list)
    second = index_repo(conn, "demo", "0.1", str(repo), repos_dir=repos_dir)
    assert second["cached"] == 5 and second["parsed"] == 0
    n_calls = conn.execute("SELECT count(*) FROM call_instances").fetchone()[0]
    assert n_calls == len(calls)
