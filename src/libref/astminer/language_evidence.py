"""Pure language evidence resolution for documentation code blocks.

Evidence is consumed by reliability: an explicit fence tag beats an ancestor
class, which beats nearby prose, which beats a configured rule. Acquisition-time
candidates are only tried by the caller when none of those facts exists.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from libref.astminer.config import LANGUAGE_CONFIGS

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
    "golang": "go",
    "java": "java",
    "kt": "kotlin",
    "kts": "kotlin",
    "groovy": "groovy",
    "c": "c",
    "cpp": "cpp",
    "cc": "cpp",
    "c++": "cpp",
    "sh": "shell",
    "bash": "shell",
    "shell": "shell",
    "console": "shell",
    "json": "json",
    "html": "html",
    "xml": "xml",
    "toml": "toml",
    "yaml": "yaml",
    "yml": "yaml",
    "diff": "diff",
    "text": "text",
    "txt": "text",
    "plaintext": "text",
}

# A prose hint needs an explicit language word; an ordinary English word such
# as "go" must not turn an unrelated example into a Go snippet.
_PROSE_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("python", "python"),
    ("java script", "javascript"),
    ("javascript", "javascript"),
    ("type script", "typescript"),
    ("typescript", "typescript"),
    ("kotlin", "kotlin"),
    ("groovy", "groovy"),
    ("rust", "rust"),
    ("c sharp", "csharp"),
    ("c#", "csharp"),
    ("c plus plus", "cpp"),
    ("c++", "cpp"),
    ("golang", "go"),
    ("java", "java"),
    ("go", "go"),
)
_CONTEXT_WORDS = r"(?:code|example|snippet|client|api|dsl|sample|usage|script)"
_PROSE_PATTERNS = tuple(
    (
        language,
        re.compile(
            rf"(?:\b{re.escape(word)}\b[^.\n]{{0,24}}\b{_CONTEXT_WORDS}\b"
            rf"|\b{_CONTEXT_WORDS}\b[^.\n]{{0,24}}\b{re.escape(word)}\b)",
            re.IGNORECASE,
        ),
    )
    for word, language in _PROSE_LANGUAGES
)


@dataclass(frozen=True)
class LanguageDecision:
    """Explicit evidence found for one block, or the candidates left to try."""

    language: str = ""
    source: str = ""
    rule: str = ""
    candidates: tuple[str, ...] = ()

    @property
    def is_explicit(self) -> bool:
        return bool(self.language and self.source)


@dataclass(frozen=True)
class LanguageFacts:
    """Raw evidence collected from a block and its immediate documentation context."""

    block_tag: str = ""
    ancestor_tag: str = ""
    nearby_text: str = ""
    selector: str = ""
    url: str = ""
    manual_rules: Mapping[str, str] = field(default_factory=dict)
    candidates: tuple[str, ...] = ()


def normalize_language_tag(tag: str) -> str:
    """Normalize a raw tag, including language names without a syntax config."""
    cleaned = str(tag or "").strip().lower().replace(",", " ").replace(";", " ")
    if not cleaned:
        return ""
    value = cleaned.split()[0]
    for prefix in ("language-", "lang-", "highlight-", "code-"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    return _LANGUAGE_ALIASES.get(value, value)


def canonical_language(tag: str) -> str:
    """Map a documentation language tag to a configured language name."""
    value = normalize_language_tag(tag)
    return value if value in LANGUAGE_CONFIGS or value in ("c", "cpp") else ""


def language_from_text(text: str) -> str:
    """Read a language word that explicitly describes nearby code."""
    for line in str(text or "").splitlines():
        stripped = line.strip().strip("*#`> ")
        if not stripped:
            continue
        exact = normalize_language_tag(stripped)
        if exact and len(stripped) <= 16 and not re.search(r"\s", stripped):
            return exact
        for language, pattern in _PROSE_PATTERNS:
            if pattern.search(stripped):
                return language
    return ""


def _rule_matches(pattern: str, url: str, selector: str) -> bool:
    kind, _, expression = pattern.partition(":")
    if kind in ("url", "selector"):
        target = url if kind == "url" else selector
        if not expression:
            return False
        try:
            return re.search(expression, target, re.IGNORECASE) is not None
        except re.error:
            return expression.lower() in target.lower()
    return bool(re.search(pattern, url, re.IGNORECASE)) or bool(
        re.search(pattern, selector, re.IGNORECASE)
    )


def resolve_language(facts: LanguageFacts) -> LanguageDecision:
    """Resolve one block without parsing: candidates stay separate for validation."""
    for tag, source in (
        (facts.block_tag, "block_tag"),
        (facts.ancestor_tag, "ancestor_class"),
    ):
        language = canonical_language(tag)
        if language:
            return LanguageDecision(language=language, source=source)
        if normalize_language_tag(tag):
            return LanguageDecision(language=normalize_language_tag(tag), source=source)
    nearby = language_from_text(facts.nearby_text)
    if nearby:
        source = (
            "nearby_heading"
            if facts.nearby_text.strip().startswith("#")
            else "nearby_text"
        )
        if canonical_language(nearby):
            return LanguageDecision(language=canonical_language(nearby), source=source)
        return LanguageDecision(language=nearby, source=source)
    for pattern, language in sorted(facts.manual_rules.items()):
        if _rule_matches(pattern, facts.url, facts.selector):
            return LanguageDecision(
                language=canonical_language(language)
                or normalize_language_tag(language),
                source="manual_rule",
                rule=pattern,
            )
    candidates = tuple(
        dict.fromkeys(
            canonical_language(value)
            for value in facts.candidates
            if canonical_language(value)
        )
    )
    return LanguageDecision(candidates=candidates)
