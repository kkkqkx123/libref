"""HTML code-block extraction for documentation usage samples."""

from __future__ import annotations

from selectolax.lexbor import LexborHTMLParser

from libref.astminer.code_blocks import canonical_language
from libref.astminer.models import CodeSnippet


def html_code_blocks(
    html: str,
    *,
    sample_prefix: str,
    url: str = "",
    heading: str = "",
) -> list[CodeSnippet]:
    """Extract language-tagged pre/code blocks from cleaned documentation HTML."""
    if not html:
        return []
    try:
        tree = LexborHTMLParser(html)
    except Exception:  # noqa: BLE001 — malformed HTML yields no code blocks
        return []
    out: list[CodeSnippet] = []
    search_at = 0
    for ordinal, node in enumerate(tree.css("pre code, pre"), start=1):
        classes = " ".join(str(node.attributes.get("class") or "").split())
        language = ""
        for part in classes.split():
            if part.startswith("language-"):
                language = canonical_language(part[len("language-") :])
                break
            if part.startswith("lang-"):
                language = canonical_language(part[len("lang-") :])
                break
        if not language:
            language = canonical_language(str(node.attributes.get("data-lang") or ""))
        if not language:
            continue
        raw_code = (node.text() or "").strip()
        if not raw_code:
            continue
        found = html.find(raw_code, search_at)
        if found < 0:
            found = html.find(raw_code)
        if found >= 0:
            search_at = found + len(raw_code)
        start_line = html.count("\n", 0, found) + 1 if found >= 0 else 0
        end_line = html.count("\n", 0, found + len(raw_code)) + 1 if found >= 0 else 0
        out.append(
            CodeSnippet(
                sample_id=f"{sample_prefix}#{ordinal}",
                language=language,
                code=raw_code,
                url=url,
                heading=heading,
                ordinal=ordinal,
                origin_kind="html_code_block",
                origin_location=url,
                origin_start_byte=found,
                origin_end_byte=found + len(raw_code) if found >= 0 else 0,
                origin_start_line=start_line,
                origin_end_line=end_line,
            )
        )
    return out
