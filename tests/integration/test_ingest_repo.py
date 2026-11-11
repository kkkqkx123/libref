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


def test_lookup_prefers_override_and_expires_cache(conn, tmp_path, monkeypatch):
    import time

    from libref.services.code.repo_source import lookup_index_source_cached
    from libref.services.registry.resolver import write_cache
    from tests.conftest import make_registry_settings

    write_cache(
        conn,
        ecosystem="pypi",
        name="requests",
        docs_url=None,
        candidates=[],
        versions=["1.0"],
        stable="1.0",
        homepage=None,
        repo_url="https://github.com/org/repo",
        fetched_at=time.time(),
    )
    assert lookup_index_source_cached(conn, "pypi", "requests", "1.0") == (
        "https://github.com/org/repo",
        "registry-cache",
    )
    settings = make_registry_settings(monkeypatch, tmp_path)
    assert lookup_index_source_cached(conn, "pypi", "requests", "1.0", settings) == (
        "https://github.com/org/repo",
        "registry-cache",
    )
    conn.execute(
        "UPDATE registry_cache SET fetched_at=? WHERE ecosystem='pypi' AND name='requests'",
        (time.time() - 10 * 86400,),
    )
    conn.commit()
    assert lookup_index_source_cached(conn, "pypi", "requests", "1.0", settings) is None
    override_file = tmp_path / "overrides.json"
    override_file.write_text(
        json.dumps(
            [
                {
                    "ecosystem": "pypi",
                    "name": "requests",
                    "repo_url": "https://github.com/org/override",
                    "source": "file",
                    "verified_at": "2026-11-01",
                }
            ]
        ),
        encoding="utf-8",
    )
    file_settings = make_registry_settings(
        monkeypatch, tmp_path, registry_overrides_file=override_file
    )
    assert lookup_index_source_cached(
        conn, "pypi", "requests", "1.0", file_settings
    ) == ("https://github.com/org/override", "override")


def test_same_source_ignores_trailing_slash(conn, tmp_path, monkeypatch):
    repo = copy_mini_repo(tmp_path)
    repos_dir = tmp_path / "repos"
    index_repo(conn, "demo", "0.1", str(repo), repos_dir=repos_dir)
    second = index_repo(conn, "demo", "0.1", str(repo) + "/", repos_dir=repos_dir)
    assert second["files"] == 5


def test_ensure_residual_dir_handling(conn, tmp_path):
    from libref.astminer.repos import RepoManager

    mgr = RepoManager(tmp_path / "repos")
    dirty = tmp_path / "repos" / "custom" / "demo" / "1.0"
    dirty.mkdir(parents=True)
    (dirty / "leftover.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not a git checkout"):
        mgr.ensure(conn, "custom/demo/1.0", "https://example.com/repo.git")
    empty = tmp_path / "repos" / "custom" / "demo" / "2.0"
    empty.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="clone failed"):
        mgr.ensure(conn, "custom/demo/2.0", "https://example.com/repo.git")


def test_ensure_local_git_records_head(conn, tmp_path):
    from dulwich import porcelain

    from libref.astminer.repos import RepoManager

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("x = 1\n", encoding="utf-8")
    repo = porcelain.init(str(src))
    porcelain.add(str(src), paths=["a.py"])
    porcelain.commit(str(src), message=b"init", author=b"t <t@t>", committer=b"t <t@t>")
    expected = repo.head().decode()
    repo.close()
    mgr = RepoManager(tmp_path / "repos")
    local, commit = mgr.ensure(conn, "demo", str(src))
    assert local == src.resolve()
    assert commit == expected
