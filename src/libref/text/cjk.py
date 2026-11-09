"""Shared Chinese segmentation: jieba search tokens plus bigrams.

Both index cleaning and query shaping call the expander, keeping
the two sides symmetric by construction.
"""

from __future__ import annotations

import re

import jieba

CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]+")


def _bigrams(run: str) -> list[str]:
    return [run[i : i + 2] for i in range(len(run) - 1)] if len(run) > 1 else []


def expand_cjk_run(run: str) -> list[str]:
    """Original plus jieba search tokens plus character bigrams, deduped."""
    if not run:
        return []
    if len(run) == 1:
        return [run]
    out: list[str] = []
    seen: set[str] = set()

    def _push(term: str) -> None:
        if term and term not in seen:
            seen.add(term)
            out.append(term)

    _push(run)
    for token in jieba.cut_for_search(run):
        _push(token.strip())
    for gram in _bigrams(run):
        _push(gram)
    return out
