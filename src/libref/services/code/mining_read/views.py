"""Shared insight view sets and parameter requirement rules.

Single source of truth for the view vocabulary and the view-to-argument
dependency rules of the mining insight read paths, consumed by both the
Web and MCP exits so validation cannot drift between them.
"""

from __future__ import annotations

API_VIEWS = ("usage", "related", "args", "common")
PATTERN_VIEWS = (
    "patterns",
    "evidence",
    "tree",
    "clusters",
    "health",
    "labels",
    "snippets",
    "facets",
    "feature_summary",
    "cluster_labels",
    "drift",
)
DOC_VIEWS = ("features", "links", "coverage", "consistency")
SYMBOL_VIEWS = ("detail", "file", "children", "signature")
CALL_VIEWS = ("callee", "caller", "file")
IMPORT_VIEWS = ("importers", "imports")

_API_INSIGHT_VIEWS_REQUIRING_API = ("related", "args")
_SYMBOL_VIEWS_REQUIRING_API = ("file", "children", "signature")


def validate_api_insight(view: str, api: str | None, apis: list[str] | None) -> None:
    """Check api/apis requirements for the api-centric usage views."""
    if view in _API_INSIGHT_VIEWS_REQUIRING_API and not api:
        raise ValueError(f"{view} view requires api")
    if view == "common" and not api and not apis:
        raise ValueError("common view requires api or apis")


def validate_pattern_insight(view: str, pattern: list[str] | None) -> None:
    """Check pattern requirements for the sequence insight views."""
    if view == "evidence" and not pattern:
        raise ValueError("evidence view requires a non-empty pattern")


def validate_symbol_insight(view: str, api: str | None, apis: list[str] | None) -> None:
    """Check api/apis requirements for the symbol detail views."""
    if view == "detail" and not api and not apis:
        raise ValueError("detail view requires api or apis")
    if view in _SYMBOL_VIEWS_REQUIRING_API and not api:
        raise ValueError(f"{view} view requires api")


def require_api(view: str, api: str | None) -> None:
    """Check that an api-scoped view received its api argument."""
    if not api:
        raise ValueError(f"{view} view requires api")
