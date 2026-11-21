"""Result-count bounds shared by every exit.

The same knob had four different upper bounds depending on which exit rejected
it, so a caller could not tell whether 150 rows were allowed. Bounds live here
once; MCP tools and HTTP routes both read them.
"""

from __future__ import annotations

from libref.errors import LibrefError

SEARCH_RESULTS = 100
"""Rows a retrieval call may ask for."""

ANSWER_MATERIALS = 50
"""Passages a grounded answer may assemble."""

LISTING_ROWS = 200
"""Records a listing call (jobs, index requests, reviews) may return."""

INSIGHT_ROWS = 500
"""Rows a mining-insight call may return."""

PER_FILE_ROWS = 20
"""Occurrences an insight view may read from one file."""


def require_limit(
    value: int | None, *, high: int, low: int = 1, name: str = "limit"
) -> None:
    """Reject an out-of-range count with the one error every exit renders."""
    if value is None or low <= value <= high:
        return
    raise LibrefError(
        "invalid_argument", f"{name} must be between {low} and {high}", field=name
    )
