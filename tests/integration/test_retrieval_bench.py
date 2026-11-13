"""Offline retrieval benchmark gate over the frozen fixture archive.

Rebuilds the dataset from the archive acquired by `fetch.py`, runs the
production retrieval paths and checks the versioned gates.

Opt-in via ``LIBREF_RUN_BENCH=1``: the rebuild is offline but heavy (it indexes
a whole documentation site and a whole source tree). The archive is not
versioned, so a machine without it skips instead of failing. Reports for human
review come from ``uv run python -m tests.e2e.bench.evaluate``.
"""

from __future__ import annotations

import os

import pytest

from tests.e2e.bench.archive import missing

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("LIBREF_RUN_BENCH") != "1",
        reason="retrieval benchmark needs LIBREF_RUN_BENCH=1",
    ),
    pytest.mark.skipif(
        bool(missing()),
        reason="benchmark archive absent: run `uv run python -m tests.e2e.bench.fetch`",
    ),
]


def test_retrieval_benchmark_gates(tmp_path) -> None:
    from tests.e2e.bench.build import build
    from tests.e2e.bench.evaluate import run

    data_dir = tmp_path / "data"
    manifest = build(data_dir)
    fixtures = manifest["fixtures"]
    names = list(fixtures)
    by_kind = {info["kind"]: name for name, info in fixtures.items()}
    assert fixtures[by_kind["docs"]]["pages"] > 0
    assert fixtures[by_kind["code"]]["patterns"] > 0
    result = run(data_dir, tmp_path / "report", probes_enabled=False)
    assert result["failures"] == []
    summary = result["summary"]
    for corpus in names:
        assert summary[corpus]["precise"]["all"]["recall_at_k"] > 0
