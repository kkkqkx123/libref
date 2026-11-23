"""Deterministic document-block facets for structure and content mining.

The module transforms plain block records into facet rows without touching the
database or any crawler type. Every emitted facet uses one value shape so the
read model and the retrieval scorer need only one decoder.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

FACET_BLOCK_TYPE = "block_type"
FACET_HEADING_DEPTH = "heading_depth"
FACET_LENGTH_BUCKET = "length_bucket"
FACET_CODE_RATIO = "code_ratio"
FACET_HAS_TABLE = "has_table"
FACET_DEPRECATED = "deprecated"
FACET_VERSION_MARKER = "version_marker"
FACET_EXAMPLE_LANGUAGE = "example_language"
FACET_EXAMPLE_SYMBOLS = "example_symbols"
FACET_PARAM_NAMES = "param_names"
FACET_RETURN_HINT = "return_hint"
FACET_TERM_CANDIDATES = "term_candidates"

DOC_FACETS: tuple[str, ...] = (
    FACET_BLOCK_TYPE,
    FACET_HEADING_DEPTH,
    FACET_LENGTH_BUCKET,
    FACET_CODE_RATIO,
    FACET_HAS_TABLE,
    FACET_DEPRECATED,
    FACET_VERSION_MARKER,
    FACET_EXAMPLE_LANGUAGE,
    FACET_EXAMPLE_SYMBOLS,
    FACET_PARAM_NAMES,
    FACET_RETURN_HINT,
    FACET_TERM_CANDIDATES,
)

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*(.*)$")
_TABLE_RE = re.compile(r"^\s*\|.*\|\s*$")
_VERSION_RE = re.compile(r"\b\d+\.\d+(?:\.\d+)?(?:[-+A-Za-z0-9.]+)?\b")
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.-]{2,}\b")
_DEPRECATED_WORDS = (
    "deprecated",
    "deprecation",
    "不再支持",
    "废弃",
    "已弃用",
    "no longer supported",
    "removed in",
)
_WARNING_WORDS = ("warning", "caution", "注意", "警告", "danger", "important")
_PARAM_WORDS = ("parameter", "argument", "参数", "params", "options")
_RETURN_WORDS = ("return", "returns", "返回值", "返回")
_CHANGELOG_WORDS = ("changelog", "change log", "release notes", "更新日志", "变更日志")
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "into",
        "your",
        "you",
        "are",
        "not",
        "can",
        "will",
        "have",
        "has",
        "use",
        "using",
        "see",
        "more",
        "http",
        "https",
        "www",
        "com",
        "org",
    }
)


@dataclass
class DocBlock:
    """One persisted document chunk reduced to the fields the facets need."""

    document_id: int
    content: str
    url: str = ""
    path: str = ""
    title: str = ""
    chunk_path: str = "embedding"


@dataclass
class DocFacet:
    """One facet row ready for bulk persistence."""

    document_id: int
    facet: str
    value: dict[str, Any]


@dataclass
class DocFacetResult:
    """Facet rows plus the count of candidates rejected by the budget."""

    rows: list[DocFacet] = field(default_factory=list)
    dropped: int = 0


def _lower(text: str) -> str:
    return str(text or "").lower()


def _contains_any(text: str, words: tuple[str, ...] | list[str]) -> bool:
    low = _lower(text)
    return any(word in low for word in words)


def _fence_spans(content: str) -> tuple[list[str], int]:
    """Return fence language tags and the number of code characters."""
    languages: list[str] = []
    code_chars = 0
    marker = ""
    for line in content.splitlines():
        match = _FENCE_RE.match(line)
        if match is not None:
            current = match.group(1)
            if not marker:
                marker = current[0] * len(current)
                tag = (
                    match.group(2).strip().split()[0] if match.group(2).strip() else ""
                )
                if tag:
                    languages.append(_lower(tag))
            elif current[0] == marker[0] and len(current) >= len(marker):
                marker = ""
            continue
        if marker:
            code_chars += len(line)
    return languages, code_chars


def fence_codes(content: str) -> list[str]:
    """Return normalized code bodies in fence order, for snippet identity matching."""
    codes: list[str] = []
    body: list[str] = []
    marker = ""
    for line in str(content or "").splitlines():
        match = _FENCE_RE.match(line)
        if match is not None:
            current = match.group(1)
            if not marker:
                marker = current[0]
                body = []
            elif current[0] == marker[0]:
                code = "\n".join(body).strip()
                if code:
                    codes.append(code)
                marker = ""
            continue
        if marker:
            body.append(line)
    return codes


def code_hash(code: str) -> str:
    """Identity of one snippet body, insensitive to whitespace reflow."""
    import hashlib

    normalized = " ".join(str(code or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _block_type(content: str, path: str, has_table: bool, has_fence: bool) -> str:
    heading = f"{path} {_extract_heading(content)}"
    if _contains_any(heading, _CHANGELOG_WORDS):
        return "changelog"
    if _contains_any(heading, _WARNING_WORDS) or _contains_any(
        content[:200], _WARNING_WORDS
    ):
        return "warning"
    if has_table and _contains_any(heading, _PARAM_WORDS):
        return "params"
    if has_fence:
        return "example"
    return "narrative"


def _extract_heading(content: str) -> str:
    for line in content.splitlines()[:4]:
        match = re.match(r"^#{1,6}\s+(.*)$", line)
        if match is not None:
            return match.group(1)
    return ""


def _length_bucket(length: int) -> str:
    if length < 300:
        return "short"
    if length < 1200:
        return "medium"
    return "long"


def _terms(content: str, limit: int) -> list[str]:
    counts: Counter[str] = Counter()
    for raw in _IDENT_RE.findall(content):
        token = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw).lower()
        if (
            len(token) < 3
            or token in _STOPWORDS
            or token.startswith(("http_", "https_"))
        ):
            continue
        counts[token] += 1
    return [term for term, _ in counts.most_common(limit)]


def _field_terms(content: str, words: tuple[str, ...], limit: int) -> list[str]:
    lines = content.splitlines()
    picked: list[str] = []
    active = False
    for line in lines:
        heading = re.match(r"^#{1,6}\s+(.*)$", line)
        if heading is not None:
            active = _contains_any(heading.group(1), words)
            continue
        if not active:
            continue
        if re.match(r"^#{1,6}\s+", line):
            break
        picked.extend(_terms(line, limit))
    return list(dict.fromkeys(picked))[:limit]


def aggregate_doc_facets(
    blocks: list[DocBlock],
    *,
    enabled_facets: list[str] | tuple[str, ...] | None = None,
    terms_per_facet: int = 20,
    candidate_budget: int = 200,
    snippet_symbols: dict[int, list[str]] | None = None,
) -> DocFacetResult:
    """Build document facets in a fixed order, dropping whole facets over budget."""
    enabled = [name for name in (enabled_facets or DOC_FACETS) if name in DOC_FACETS]
    result = DocFacetResult()
    symbols = snippet_symbols or {}
    priority = {name: index for index, name in enumerate(DOC_FACETS)}
    for block in blocks:
        content = str(block.content or "")
        languages, code_chars = _fence_spans(content)
        has_fence = bool(languages) or code_chars > 0
        has_table = any(_TABLE_RE.match(line) for line in content.splitlines()) or (
            "<table" in _lower(content)
        )
        kind = _block_type(content, block.path, has_table, has_fence)
        depth = len([part for part in block.path.split("/") if part.strip()])
        ratio = min(1.0, round(code_chars / len(content), 4)) if content else 0.0
        candidates: dict[str, dict[str, Any]] = {
            FACET_BLOCK_TYPE: {"kind": kind, "details": {"value": kind}},
            FACET_HEADING_DEPTH: {"kind": depth, "details": {"value": depth}},
            FACET_LENGTH_BUCKET: {
                "kind": _length_bucket(len(content)),
                "details": {"value": _length_bucket(len(content))},
            },
            FACET_CODE_RATIO: {"kind": "ratio", "details": {"value": ratio}},
            FACET_HAS_TABLE: {"kind": "flag", "details": {"value": has_table}},
            FACET_DEPRECATED: {
                "kind": "flag",
                "details": {"value": _contains_any(content, _DEPRECATED_WORDS)},
            },
            FACET_VERSION_MARKER: {
                "kind": "versions",
                "details": {
                    "value": list(dict.fromkeys(_VERSION_RE.findall(content)))[
                        :terms_per_facet
                    ]
                },
            },
            FACET_EXAMPLE_LANGUAGE: {
                "kind": "languages",
                "details": {"value": list(dict.fromkeys(languages))[:terms_per_facet]},
            },
            FACET_EXAMPLE_SYMBOLS: {
                "kind": "symbols",
                "details": {
                    "value": list(dict.fromkeys(symbols.get(block.document_id, [])))[
                        :terms_per_facet
                    ]
                },
            },
            FACET_PARAM_NAMES: {
                "kind": "params",
                "details": {
                    "value": _field_terms(content, _PARAM_WORDS, terms_per_facet)
                },
            },
            FACET_RETURN_HINT: {
                "kind": "returns",
                "details": {
                    "value": _field_terms(content, _RETURN_WORDS, terms_per_facet)
                },
            },
            FACET_TERM_CANDIDATES: {
                "kind": "terms",
                "details": {"value": _terms(content, terms_per_facet)},
            },
        }
        kept: list[str] = []
        for name in sorted(enabled, key=lambda item: priority[item]):
            value = candidates[name]
            empty = value["details"].get("value") in ("", [], False, 0)
            if empty and name not in (
                FACET_BLOCK_TYPE,
                FACET_HEADING_DEPTH,
                FACET_LENGTH_BUCKET,
                FACET_CODE_RATIO,
                FACET_HAS_TABLE,
                FACET_DEPRECATED,
            ):
                continue
            if len(kept) >= candidate_budget:
                result.dropped += 1
                continue
            kept.append(name)
            result.rows.append(DocFacet(block.document_id, name, value))
    return result
