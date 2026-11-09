"""Embedding text assembly: strip markdown marks, prepend title and path."""

from __future__ import annotations

import re

_FENCE_DELIM_RE = re.compile(r"^\s*(```|~~~)")
_HEADING_MARK_RE = re.compile(r"^\s*#{1,6}\s+")
_QUOTE_MARK_RE = re.compile(r"^\s*>\s?")
_LIST_MARK_RE = re.compile(r"^(\s*)[-*+]\s+")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BOLD_RE = re.compile(r"(\*\*|__)(.*?)\1")
_TILDE_RE = re.compile(r"~~(.*?)~~")
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")


def strip_markdown_for_embedding(text: str | None) -> str:
    """Remove markdown marks before encoding, keeping words and code content."""
    if not text:
        return ""
    kept: list[str] = []
    for line in text.splitlines():
        if _FENCE_DELIM_RE.match(line):
            continue
        line = _IMAGE_RE.sub(r"\1", line)
        line = _LINK_RE.sub(r"\1", line)
        line = _HEADING_MARK_RE.sub("", line)
        line = _QUOTE_MARK_RE.sub("", line)
        line = _LIST_MARK_RE.sub(r"\1", line)
        line = _CODE_SPAN_RE.sub(r"\1", line)
        line = _BOLD_RE.sub(r"\2", line)
        line = _TILDE_RE.sub(r"\1", line)
        kept.append(line.rstrip())
    out: list[str] = []
    blank = 0
    for line in kept:
        if not line.strip():
            blank += 1
            if blank > 2:
                continue
        else:
            blank = 0
        out.append(line)
    return "\n".join(out).strip()


def build_embed_text(
    title: str | None,
    path: str | None,
    content: str | None,
) -> str:
    """Assemble encoding text with title and path first, body stripped."""
    head = " / ".join(p for p in (title, path) if p)
    body = strip_markdown_for_embedding(content)
    return f"{head}\n{body}" if head else body
