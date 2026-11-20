"""Pure extraction of fenced code blocks from documentation text."""

from __future__ import annotations

import re
from collections.abc import Iterable

from libref.astminer.config import LANGUAGE_CONFIGS
from libref.astminer.models import CodeSnippet

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_RUST_DOC_RE = re.compile(r"^\s*(///|//!) ?(.*)$")
_LANGUAGE_ALIASES = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "rs": "rust",
    "csharp": "csharp",
    "cs": "csharp",
    "go": "go",
    "java": "java",
    "kt": "kotlin",
    "kts": "kotlin",
    "groovy": "groovy",
    "c": "c",
    "cpp": "cpp",
    "cc": "cpp",
    "c++": "cpp",
}
_SKIP_TAGS = {
    "compile_fail",
    "ignore",
    "text",
    "txt",
    "shell",
    "bash",
    "console",
    "output",
    "diff",
}


def canonical_language(tag: str) -> str:
    """Map a documentation language tag to a configured language name."""
    cleaned = str(tag or "").strip().lower().replace(",", " ").replace(";", " ")
    value = cleaned.split()[0] if cleaned else ""
    value = _LANGUAGE_ALIASES.get(value, value)
    if value in LANGUAGE_CONFIGS or value in ("c", "cpp"):
        return value
    return ""


def fenced_blocks(
    text: str,
    *,
    sample_prefix: str,
    url: str = "",
    heading: str = "",
    origin_kind: str = "markdown_fence",
) -> list[CodeSnippet]:
    """Extract balanced fenced blocks with a configured language tag."""
    lines = str(text or "").splitlines()
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
        tags = tag.lower().replace(",", " ").split()
        if any(item in _SKIP_TAGS for item in tags):
            index += 1
            while index < len(lines) and not _is_close(
                lines[index], close_char, close_len
            ):
                index += 1
            index += 1
            continue
        language = canonical_language(tag)
        body: list[str] = []
        index += 1
        while index < len(lines) and not _is_close(lines[index], close_char, close_len):
            body.append(lines[index])
            index += 1
        index += 1
        if not language or not any(line.strip() for line in body):
            continue
        ordinal += 1
        code = "\n".join(body).strip()
        if not code:
            continue
        out.append(
            CodeSnippet(
                sample_id=f"{sample_prefix}#{ordinal}",
                language=language,
                code=code,
                url=url,
                heading=heading,
                ordinal=ordinal,
                origin_kind=origin_kind,
            )
        )
    return out


def rust_doc_blocks(text: bytes | str, *, path: str) -> list[CodeSnippet]:
    """Extract fenced Rust examples embedded in doc comments."""
    raw = text.decode("utf-8", "replace") if isinstance(text, bytes) else text
    stripped: list[str] = []
    for line in raw.splitlines():
        match = _RUST_DOC_RE.match(line)
        if match is not None:
            stripped.append(match.group(2))
        else:
            stripped.append("")
    return fenced_blocks(
        "\n".join(stripped),
        sample_prefix=f"rustdoc:{path}",
        origin_kind="rustdoc_doctest",
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
        key = (item.language, re.sub(r"\s+", " ", item.code).strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
