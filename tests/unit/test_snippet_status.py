"""Snippet parse statuses distinguish weak evidence from silent drops."""

from __future__ import annotations

import pytest

from libref.astminer.models import ParseEvidence
from libref.astminer.snippet_status import classify_snippet_status

pytestmark = pytest.mark.unit


def test_all_snippet_statuses_are_reachable() -> None:
    good = ParseEvidence(call_count=1)
    assert (
        classify_snippet_status(
            good, language="python", configured=True, target_calls=1
        )
        == "ok"
    )
    assert (
        classify_snippet_status(
            ParseEvidence(call_count=1, has_error=True),
            language="python",
            configured=True,
            target_calls=1,
        )
        == "partial"
    )
    assert (
        classify_snippet_status(None, language="shell", configured=False)
        == "unsupported"
    )
    assert classify_snippet_status(None, language="", raw_language="") == "unlabeled"
    assert (
        classify_snippet_status(
            None,
            language_source="candidate",
            raw_language="python+javascript",
            usable_candidates=2,
        )
        == "ambiguous"
    )
    assert (
        classify_snippet_status(None, language_source="candidate", usable_candidates=0)
        == "failed"
    )
    assert (
        classify_snippet_status(good, language="python", configured=True)
        == "non_target"
    )
