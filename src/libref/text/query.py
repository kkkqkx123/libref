"""FTS query shaping: turn user input into syntax-safe MATCH queries."""

from __future__ import annotations

import re

from libref.text.cjk import CJK_RUN_RE, expand_cjk_run
from libref.text.ident import snake_variant


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
