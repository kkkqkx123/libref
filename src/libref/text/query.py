"""FTS query shaping: turn user input into syntax-safe MATCH queries."""

from __future__ import annotations

import re

from libref.text.cjk import CJK_RUN_RE, expand_cjk_run
from libref.text.ident import snake_variant


def query_units(query: str) -> list[str]:
    """Searchable units of a query: lowercased latin tokens plus CJK words.

    Coverage checks compare these units against candidate text, so the CJK side
    is segmented into words with the same segmenter the index uses: a whole
    CJK run never occurs verbatim in a natural question, while expanded bigrams
    make coverage look satisfied by any single common character pair. Single
    characters are dropped for the same reason.
    """
    import jieba  # type: ignore[import-untyped]

    out: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9_]+", query or ""):
        lowered = token.lower()
        if lowered and lowered not in seen:
            seen.add(lowered)
            out.append(lowered)
    for run in CJK_RUN_RE.findall(query or ""):
        for word in jieba.cut_for_search(run):
            piece = word.strip()
            if len(piece) >= 2 and piece not in seen:
                seen.add(piece)
                out.append(piece)
    return out


def safe_fts_query(query: str) -> str:
    """Convert user input into a syntax-safe FTS query, empty when no tokens."""
    latin = re.findall(r"[A-Za-z0-9_]+", query or "")
    cjk = CJK_RUN_RE.findall(query or "")
    seen: set[str] = set()
    parts: list[str] = []
    for token in latin:
        lowered = token.lower()
        if lowered and lowered not in seen:
            seen.add(lowered)
            parts.append(f'"{token}"')
        snake = snake_variant(token)
        if snake and snake not in seen:
            seen.add(snake)
            parts.append(f'"{snake}"')
    for run in cjk:
        for term in expand_cjk_run(run):
            if term not in seen:
                seen.add(term)
                parts.append(f'"{term}"')
    return " OR ".join(parts)
