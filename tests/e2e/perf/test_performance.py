"""Opt-in gate over the most recent performance report.

The test never times code itself; it checks that a report was produced, that
every case carried samples or an explicit skip note, and that no audit failure
was recorded. Timing regressions stay advisory unless the runner uses enforce.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

LATEST = Path(__file__).resolve().parent / "outputs" / "latest.json"

pytestmark = [
    pytest.mark.performance,
    pytest.mark.skipif(
        os.environ.get("LIBREF_RUN_PERF") != "1",
        reason="performance gate requires LIBREF_RUN_PERF=1",
    ),
    pytest.mark.skipif(not LATEST.is_file(), reason="no performance report yet"),
]


def test_latest_performance_report_is_complete() -> None:
    report = json.loads(LATEST.read_text(encoding="utf-8"))
    assert report["tool"] == "libref-perf"
    assert report["environment"]["python"]
    assert report["cases"]
    for case in report["cases"]:
        assert case["case_id"]
        assert case["variant"]
        if case.get("notes") and "skipped" in " ".join(case["notes"]):
            continue
        samples = case.get("samples_ms") or []
        if not samples or not any(samples):
            continue
        assert case.get("stats", {}).get("median_ms", 0) > 0, case
    assert report["failures"] == []
