"""BM25 index cleaning: light link and markup denoising."""

from __future__ import annotations

import re

_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BARE_URL_RE = re.compile(r"https?://[^\s)>\]]+")
_ANGLE_URL_RE = re.compile(r"<(https?://[^<>\s]+)>")
_QUOTE_CHARS_RE = re.compile(r"[\"'`]")


def clean_text_for_bm25(text: str | None) -> str:
    """Light denoising before FTS indexing, preserving newlines and fences."""
    if not text:
        return ""
    out_lines: list[str] = []
    for line in text.splitlines():
        line = _IMAGE_RE.sub(r"\1", line)
        line = _LINK_RE.sub(r"\1", line)
        line = _ANGLE_URL_RE.sub(" ", line)
        line = _BARE_URL_RE.sub(" ", line)
        line = _QUOTE_CHARS_RE.sub("", line)
        out_lines.append(line.rstrip())
    out: list[str] = []
    blank = 0
    for line in out_lines:
        if not line.strip():
            blank += 1
            if blank > 2:
                continue
        else:
            blank = 0
        out.append(line)
    return "\n".join(out).strip()
