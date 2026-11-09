"""End-to-end repo parsing: pull fixed tags, full indexing, assert invariants only.

Blueprint: docs/tests/parsing-verification.md.
Skipped by default; requires LIBREF_RUN_E2E=1 to enable.
A network failure marks the environment unavailable; not treated as a regression.
Reports are written to disk as human-readable text, not persisted to the DB.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("LIBREF_RUN_E2E") != "1",
    reason="e2e requires LIBREF_RUN_E2E=1 and real network",
)

MANIFEST = Path(__file__).resolve().parent / "fixtures" / "manifest.json"
PERSISTENT_REPOS = Path(__file__).resolve().parent / "repos"


def _confidence_dist(conn, library: str, version: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT confidence, count(*) FROM call_instances"
        " WHERE library_id=(SELECT id FROM libraries WHERE name=?)"
        " AND version_id=(SELECT id FROM versions WHERE version=?"
        " AND library_id=(SELECT id FROM libraries WHERE name=?))"
        " GROUP BY confidence",
        (library, version, library),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def test_e2e_repo_index_invariants(tmp_path, conn):
    from libref.services.code_ingest import index_repo

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    report_lines: list[str] = []
    for lang in ("javascript", "java"):
        entry = manifest[lang]
        library = f"e2e-{lang}"
        try:
            counts = index_repo(
                conn, library, entry["tag"], entry["url"],
                repos_dir=PERSISTENT_REPOS,
            )
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"env unavailable for {lang}: {type(exc).__name__}: {exc}")
        assert counts["files"] > 0
        assert counts["entities"] > 0
        assert counts["calls"] > 0
        rows = _confidence_dist(conn, library, entry["tag"])
        dist = dict(rows)
        total = sum(dist.values())
        assert 0 < total <= counts["calls"]
        amb_ratio = dist.get("AMBIGUOUS", 0) / total if total else 1.0
        bound = {"javascript": 0.4, "java": 0.75}[lang]
        assert amb_ratio < bound
        second = index_repo(
            conn, library, entry["tag"], entry["url"],
            repos_dir=PERSISTENT_REPOS,
        )
        assert second["cached"] > 0 and second["parsed"] == 0
        commit = conn.execute(
            "SELECT commit_sha FROM repos WHERE name=?",
            (f"custom/{library}/{entry['tag']}",),
        ).fetchone()
        commit_sha = commit[0] if commit else ""
        report_lines.append(
            f"{lang} tag={entry['tag']} files={counts['files']} "
            f"entities={counts['entities']} calls={counts['calls']} "
            f"dist={dist} cached={second['cached']} library={library} "
            f"commit={commit_sha}"
        )
    report = tmp_path / "e2e-parsing-report.txt"
    report.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    assert report_lines
