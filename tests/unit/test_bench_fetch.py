"""Acquisition-scope construction for benchmark fixtures."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_doc_scope_carries_path_and_url_exclusions() -> None:
    from tests.e2e.bench.fetch import _doc_scope

    scope = _doc_scope(
        {
            "path_prefixes": ["/en/latest/"],
            "exclude_patterns": ["/_sources/"],
            "follow_links": True,
            "extra_urls": ["https://example.invalid/docs/index.html"],
        }
    )
    assert scope == {
        "path_prefixes": ["/en/latest/"],
        "exclude_patterns": ["/_sources/"],
        "follow_links": True,
        "urls": ["https://example.invalid/docs/index.html"],
    }


def test_doc_scope_without_optional_fields_is_empty() -> None:
    from tests.e2e.bench.fetch import _doc_scope

    assert _doc_scope({"start_url": "https://example.invalid/docs/"}) == {}
