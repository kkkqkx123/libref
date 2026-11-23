"""The offline document-feature review renderer produces complete, non-empty reports."""

from __future__ import annotations

import pytest

from tests.e2e.render_doc_feature_outputs import REPORT_NAMES, render

pytestmark = pytest.mark.integration


def test_doc_feature_outputs(tmp_path) -> None:
    out_dir = tmp_path / "doc_features"
    summary = render(out_dir)

    for name in REPORT_NAMES:
        path = out_dir / name
        assert path.is_file(), name
        assert path.read_text(encoding="utf-8").strip(), name

    assert all(summary["invariants"].values()), summary["invariants"]
    assert all(
        summary["counts"][key] > 0 for key in ("features", "links", "consistency")
    )
    assert summary["counts"]["coverage"] > 0
    assert summary["metrics"]["questions"] > 0
    assert set(summary["metrics"]["filters"]) == {
        "examples_only",
        "exclude_deprecated",
        "aligned_only",
    }
    assert "Invariants:" in (out_dir / "SUMMARY.txt").read_text(encoding="utf-8")
