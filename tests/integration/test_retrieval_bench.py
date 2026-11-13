"""Offline retrieval benchmark gate over real project fixtures.

Rebuilds the dataset from the repository's own documentation and source trees,
runs the production retrieval paths, and checks the versioned gates.

Opt-in via ``LIBREF_RUN_BENCH=1``: the run is fully offline but heavy (it
indexes the whole doc tree and the whole source tree), so the default suite
stays fast. Reports for human review come from
``uv run python -m tests.e2e.bench.evaluate``.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("LIBREF_RUN_BENCH") != "1",
        reason="retrieval benchmark needs LIBREF_RUN_BENCH=1",
    ),
]


def test_retrieval_benchmark_gates(tmp_path) -> None:
    from tests.e2e.bench.build import build
    from tests.e2e.bench.evaluate import run

    data_dir = tmp_path / "data"
    manifest = build(data_dir)
    assert manifest["skipped"] == []
    assert manifest["fixtures"]["repo_docs"]["pages"] > 0
    assert manifest["fixtures"]["repo_code"]["patterns"] > 0
    result = run(data_dir, tmp_path / "report", probes_enabled=False)
    assert result["failures"] == []
    summary = result["summary"]
    for corpus in ("docs", "code"):
        assert summary[corpus]["precise"]["all"]["recall_at_k"] > 0
