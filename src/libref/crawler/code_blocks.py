"""HTML code-block extraction for documentation usage samples."""

from __future__ import annotations

from typing import Any

from selectolax.lexbor import LexborHTMLParser

from libref.astminer.code_blocks import import_context_from_text
from libref.astminer.language_evidence import (
    LanguageFacts,
    canonical_language,
    normalize_language_tag,
    resolve_language,
)
from libref.astminer.models import CodeSnippet

_NEARBY_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "p", "figcaption"})


def _ancestor_tag(node) -> str:
    """Read a language class or data attribute from the closest code wrapper."""
    current = node.parent
    for _ in range(4):
        if current is None or not hasattr(current, "attributes"):
            return ""
        attrs = current.attributes
        classes = " ".join(str(attrs.get("class") or "").split())
        for part in classes.split():
            for prefix in ("language-", "lang-", "highlight-", "code-"):
                if part.startswith(prefix):
                    tag = part[len(prefix) :]
                    if normalize_language_tag(tag):
                        return tag
        for key in ("data-lang", "data-language"):
            tag = str(attrs.get(key) or "")
            if normalize_language_tag(tag):
                return tag
        current = current.parent
    return ""


def _selector(node) -> str:
    parts: list[str] = []
    current = node
    for _ in range(4):
        if current is None or not hasattr(current, "attributes"):
            break
        tag = str(getattr(current, "tag", "") or "")
        classes = " ".join(str(current.attributes.get("class") or "").split())
        parts.append(f"{tag}.{classes.replace(' ', '.')}" if classes else tag)
        current = current.parent
    return " ".join(reversed(parts))


def _nearby_text(node) -> str:
    """Language words from the nearest preceding title or explanation."""
    current: Any = node
    found: list[str] = []
    for _ in range(4):
        if current is None or not hasattr(current, "prev"):
            break
        previous = current.prev
        if previous is not None and hasattr(previous, "tag"):
            tag = str(getattr(previous, "tag", "") or "")
            if tag in _NEARBY_TAGS:
                text = (previous.text() or "").strip()
                if text:
                    found.append(text)
                break
        current = current.parent
    return "\n".join(found)


def html_code_blocks(
    html: str,
    *,
    sample_prefix: str,
    url: str = "",
    heading: str = "",
    manual_rules: dict[str, str] | None = None,
) -> list[CodeSnippet]:
    """Extract HTML pre blocks with explicit, contextual and candidate language facts."""
    if not html:
        return []
    try:
        tree = LexborHTMLParser(html)
    except Exception:  # noqa: BLE001 — malformed HTML yields no code blocks
        return []
    out: list[CodeSnippet] = []
    search_at = 0
    # Origin byte offsets are UTF-8 byte offsets into the host HTML,
    # matching the byte convention used for markdown fences and rustdoc.
    for ordinal, node in enumerate(tree.css("pre"), start=1):
        raw_code = (node.text() or "").strip()
        if not raw_code:
            continue
        block_tag = ""
        for part in " ".join(str(node.attributes.get("class") or "").split()).split():
            for prefix in ("language-", "lang-", "highlight-", "code-"):
                if part.startswith(prefix) and normalize_language_tag(
                    part[len(prefix) :]
                ):
                    block_tag = part[len(prefix) :]
                    break
            if block_tag:
                break
        if not block_tag:
            for key in ("data-lang", "data-language"):
                tag = str(node.attributes.get(key) or "")
                if normalize_language_tag(tag):
                    block_tag = tag
                    break
        facts = LanguageFacts(
            block_tag=block_tag,
            ancestor_tag=_ancestor_tag(node),
            nearby_text=_nearby_text(node),
            selector=_selector(node),
            url=url,
            manual_rules=manual_rules or {},
        )
        decision = resolve_language(facts)
        found = html.find(raw_code, search_at)
        if found < 0:
            found = html.find(raw_code)
        if found >= 0:
            search_at = found + len(raw_code)
            start_byte = len(html[:found].encode("utf-8"))
            end_byte = start_byte + len(raw_code.encode("utf-8"))
            start_line = html.count("\n", 0, found) + 1
            end_line = html.count("\n", 0, found + len(raw_code)) + 1
        else:
            start_byte = 0
            end_byte = 0
            start_line = 0
            end_line = 0
        out.append(
            CodeSnippet(
                sample_id=f"{sample_prefix}#{ordinal}",
                language=canonical_language(decision.language),
                raw_language=decision.language,
                language_source=decision.source,
                language_rule=decision.rule,
                code=raw_code,
                url=url,
                heading=heading,
                ordinal=ordinal,
                origin_kind="html_code_block",
                origin_location=url,
                origin_start_byte=start_byte,
                origin_end_byte=end_byte,
                origin_start_line=start_line,
                origin_end_line=end_line,
                import_context=(
                    import_context_from_text(html[:found]) if found >= 0 else ""
                ),
            )
        )
    return out
