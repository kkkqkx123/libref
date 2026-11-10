"""Trilingual mini-repo end to end: go module prefix, rust paths, csharp calls."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from libref.services.code.code_ingest import index_repo

pytestmark = pytest.mark.integration


def _copy_tri_repo(tmp_path: Path) -> Path:
    src = Path(__file__).resolve().parent.parent / "fixtures" / "mini_repo_tri"
    dst = tmp_path / "mini_repo_tri"
    shutil.copytree(src, dst)
    return dst


def test_trilingual_ingest_end_to_end(conn, tmp_path, monkeypatch):
    repo = _copy_tri_repo(tmp_path)
    repos_dir = tmp_path / "repos"
    counts = index_repo(conn, "tri", "0.1", str(repo), repos_dir=repos_dir)
    assert counts["files"] == 5
    assert counts["parsed"] == 5

    decls = {
        row[0]
        for row in conn.execute("SELECT qualified_name FROM api_entities").fetchall()
    }
    assert "util.Fetch" in decls
    assert "call.Run" in decls
    assert "cache.Cache.new" in decls
    assert "cache.Cache.get" in decls
    assert "Demo.Greeter.Say" in decls

    calls = {
        (row[0], row[1], row[3])
        for row in conn.execute(
            "SELECT callee_name, confidence, caller_file, caller FROM call_instances"
        ).fetchall()
    }
    assert ("util.Fetch", "EXTRACTED", "call.Run") in calls
    assert ("cache.Cache.new", "EXTRACTED", "main") in calls
    assert ("Demo.Greeter.Helper", "EXTRACTED", "Demo.Greeter.Say") in calls
    assert any(
        callee == "System.Console.WriteLine" and confidence == "AMBIGUOUS"
        for callee, confidence, _caller in calls
    )

    second = index_repo(conn, "tri", "0.1", str(repo), repos_dir=repos_dir)
    assert second["cached"] == 5 and second["parsed"] == 0
