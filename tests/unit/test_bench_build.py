"""Corpus assertions applied when replaying a frozen fixture archive."""

from __future__ import annotations

import pytest

from tests.e2e.bench import archive as bench_archive
from tests.e2e.bench.build import _check_docs

pytestmark = pytest.mark.unit


def _info(path: str) -> dict:
    return {"pages": 1, "chunks": 1, "cjk_pages": 0, "page_paths": [path]}


def test_docs_exclusion_rejects_a_reintroduced_page() -> None:
    entry = {
        "corpus_lang": "en",
        "exclude_patterns": ["/_sources/"],
    }
    with pytest.raises(bench_archive.ArchiveError, match="exclude_patterns"):
        _check_docs("demo/docs", entry, _info("/en/latest/_sources/api.md.txt"))


def test_docs_exclusion_accepts_the_rendered_page() -> None:
    entry = {
        "corpus_lang": "en",
        "exclude_patterns": ["/_sources/"],
    }
    _check_docs("demo/docs", entry, _info("/en/latest/api.html"))
