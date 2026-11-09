"""Identifier snake-case helpers shared by chunking keywords and FTS queries."""

from __future__ import annotations

import re

_CAMEL_BOUND_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def ident_snake_form(name: str) -> str:
    """Convert camelCase to snake_case for cross-style alignment."""
    text = (name or "").strip()
    if not text:
        return ""
    return _CAMEL_BOUND_RE.sub("_", text).lower()


def snake_variant(token: str) -> str:
    """Return the snake_case variant of a query token, empty when identical."""
    snake = _CAMEL_BOUND_RE.sub("_", token).lower()
    return snake if snake and snake != token.lower() else ""
