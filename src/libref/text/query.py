"""FTS query shaping: turn user input into syntax-safe MATCH queries."""

from __future__ import annotations

import re

from libref.text.ident import snake_variant


def safe_fts_query(query: str) -> str:
    """Convert user input into a syntax-safe FTS query, empty when no tokens."""
    latin = re.findall(r"[A-Za-z0-9_]+", query or "")
    cjk = re.findall(r"[\u4e00-\u9fff]+", query or "")
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
    for token in cjk:
        if token not in seen:
            seen.add(token)
            parts.append(f'"{token}"')
    return " OR ".join(parts)
