"""Pure extraction of fenced code blocks from documentation text."""

from __future__ import annotations

import re
from collections.abc import Iterable

from libref.astminer.config import LANGUAGE_CONFIGS
from libref.astminer.language_evidence import (
    canonical_language,
    language_from_text,
    normalize_language_tag,
)
from libref.astminer.models import CodeSnippet

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_RUST_DOC_RE = re.compile(r"^\s*(///|//!) ?(.*)$")
_IMPORT_LINE_RE = re.compile(r"^\s*(?:import|from|using|use)\b")
_PROMPT_RE = re.compile(r"^\s*(?:>>>|\.\.\.|\$|\d+(?:\.|:)\s*)+\s*")


def strip_doc_prompt(line: str) -> str:
    """Remove interactive prompts and line-number prefixes from one line."""
    return _PROMPT_RE.sub("", str(line or ""), count=1)


def import_context_from_text(text: str) -> str:
    """Import-like lines that can give a doc snippet resolvable aliases."""
    lines: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if _IMPORT_LINE_RE.match(line):
            lines.append(line)
            continue
        prompt_free = strip_doc_prompt(line).strip()
        if prompt_free != line and _IMPORT_LINE_RE.match(prompt_free):
            lines.append(prompt_free)
    return "\n".join(lines[-32:])


def _byte_starts(lines: list[str]) -> list[int]:
    starts: list[int] = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line.encode("utf-8")) + 1
    return starts


def _body_origin(
    body: list[str],
    first_line: int,
    starts: list[int],
    origin_lines: list[tuple[int, int]] | None,
) -> tuple[int, int, int, int]:
    """Map the stripped block body back to host line and byte positions."""
    leading = 0
    while leading < len(body) and not body[leading].strip():
        leading += 1
    index = first_line + leading
    text_line = body[min(leading, len(body) - 1)]
    column = len(text_line) - len(text_line.lstrip())
    line_no, byte_at = (
        origin_lines[index]
        if origin_lines is not None and index < len(origin_lines)
        else (index + 1, starts[index] if index < len(starts) else 0)
    )
    start_byte = byte_at + column
    last = max(index, first_line + len(body) - 1)
    end_line, end_at = (
        origin_lines[last]
        if origin_lines is not None and last < len(origin_lines)
        else (last + 1, starts[last] if last < len(starts) else start_byte)
    )
    end_byte = end_at + len(body[-1].encode("utf-8")) if body else start_byte
    return line_no, end_line, start_byte, end_byte


def fenced_blocks(
    text: str,
    *,
    sample_prefix: str,
    url: str = "",
    heading: str = "",
    origin_kind: str = "markdown_fence",
    origin_location: str = "",
    origin_lines: list[tuple[int, int]] | None = None,
) -> list[CodeSnippet]:
    """Extract balanced fenced blocks with their original language evidence.

    Every non-empty fence is emitted, including blocks with no usable language,
    so an unlabeled or unsupported block can be listed instead of vanishing.
    """
    lines = str(text or "").splitlines()
    starts = _byte_starts(lines)
    out: list[CodeSnippet] = []
    ordinal = 0
    index = 0
    while index < len(lines):
        match = _FENCE_RE.match(lines[index])
        if match is None:
            index += 1
            continue
        marker, tag = match.group(1), match.group(2).strip()
        close_char = marker[0]
        close_len = len(marker)
        language = canonical_language(tag)
        raw_language = normalize_language_tag(tag)
        nearby = "\n".join(lines[max(0, index - 4) : index])
        language_source = "block_tag" if raw_language else ""
        if not language_source:
            hinted = language_from_text(nearby)
            if hinted:
                raw_language = hinted
                language = canonical_language(hinted)
                language_source = (
                    "nearby_heading"
                    if any(line.strip().startswith("#") for line in nearby.splitlines())
                    else "nearby_text"
                )
        body: list[str] = []
        body_start = index + 1
        index += 1
        while index < len(lines) and not _is_close(lines[index], close_char, close_len):
            body.append(lines[index])
            index += 1
        index += 1
        if not any(line.strip() for line in body):
            continue
        ordinal += 1
        code = "\n".join(body).strip()
        if not code:
            continue
        start_line, end_line, start_byte, end_byte = _body_origin(
            body, body_start, starts, origin_lines
        )
        out.append(
            CodeSnippet(
                sample_id=f"{sample_prefix}#{ordinal}",
                language=language,
                raw_language=raw_language,
                language_source=language_source,
                code=code,
                url=url,
                heading=heading,
                ordinal=ordinal,
                origin_kind=origin_kind,
                origin_location=origin_location or url,
                origin_start_byte=start_byte,
                origin_end_byte=end_byte,
                origin_start_line=start_line,
                origin_end_line=end_line,
                import_context=import_context_from_text("\n".join(lines[:index])),
            )
        )
    return out


def rust_doc_blocks(text: bytes | str, *, path: str) -> list[CodeSnippet]:
    """Extract fenced Rust examples embedded in doc comments."""
    raw = text.decode("utf-8", "replace") if isinstance(text, bytes) else text
    stripped: list[str] = []
    origin_lines: list[tuple[int, int]] = []
    offset = 0
    for line_no, line in enumerate(raw.splitlines(), start=1):
        match = _RUST_DOC_RE.match(line)
        content = match.group(2) if match is not None else ""
        prefix = match.start(2) if match is not None else 0
        stripped.append(content)
        origin_lines.append((line_no, offset + len(line[:prefix].encode("utf-8"))))
        offset += len(line.encode("utf-8")) + 1
    return fenced_blocks(
        "\n".join(stripped),
        sample_prefix=f"rustdoc:{path}",
        origin_kind="rustdoc_doctest",
        origin_location=path,
        origin_lines=origin_lines,
    )


def _is_close(line: str, char: str, minimum: int) -> bool:
    match = _FENCE_RE.match(line)
    if match is None:
        return False
    marker = match.group(1)
    return marker[0] == char and len(marker) >= minimum and not match.group(2).strip()


def suffix_for_language(language: str) -> str:
    """Synthetic file suffix used by the AST engine for one language."""
    if language == "c":
        return ".c"
    if language == "cpp":
        return ".cpp"
    for cfg in LANGUAGE_CONFIGS.values():
        if cfg.name == language:
            return cfg.file_suffixes[0]
    return ".txt"


def dedupe_snippets(snippets: Iterable[CodeSnippet]) -> list[CodeSnippet]:
    """Stable dedup by normalized code and language, preserving first evidence."""
    seen: set[tuple[str, str]] = set()
    out: list[CodeSnippet] = []
    for item in snippets:
        key = (
            item.language or item.raw_language,
            re.sub(r"\s+", " ", item.code).strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
