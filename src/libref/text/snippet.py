"""Snippet and material truncation: pure string windows for display."""

from __future__ import annotations

import re


def make_snippet(content: str | None, query: str, *, radius: int, cap: int) -> str:
    """Cut a context window around the first query hit (FTS snippet fallback)."""
    text = (content or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    pos = -1
    for token in re.findall(r"[\w-]+", query.lower()):
        idx = lowered.find(token)
        if idx >= 0 and (pos < 0 or idx < pos):
            pos = idx
    pos = max(pos, 0)
    start = max(0, pos - radius)
    end = min(len(text), pos + radius)
    line_start = text.rfind("\n", 0, start)
    start = line_start + 1 if line_start >= 0 else 0
    line_end = text.find("\n", end)
    end = line_end if line_end >= 0 else len(text)
    if end - start > cap:
        end = start + cap
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    window = text[start:end]
    for token in sorted(set(re.findall(r"[\w-]+", query)), key=len, reverse=True):
        pattern = re.compile(re.escape(token), re.IGNORECASE)
        window = pattern.sub(lambda m: f">>>{m.group(0)}<<<", window)
    return f"{prefix}{window}{suffix}"


def truncate_material(text: str | None, limit: int) -> str:
    """Truncate long answer material at a line boundary with an ellipsis."""
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    cut = body.rfind("\n", 0, limit)
    end = cut if cut > limit // 2 else limit
    return body[:end].rstrip() + "\n..."
