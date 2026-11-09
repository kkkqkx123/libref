"""Markdown and source normalization: blank collapsing and source wrapping."""

from __future__ import annotations


def normalize_markdown(text: str | None) -> str:
    """Collapse runs of blank lines and strip trailing whitespace."""
    lines = [line.rstrip() for line in (text or "").splitlines()]
    out: list[str] = []
    blank = 0
    for line in lines:
        if line == "":
            blank += 1
            if blank > 2:
                continue
        else:
            blank = 0
        out.append(line)
    return "\n".join(out).strip()


def wrap_source_content(raw: str | None, url: str) -> str:
    """Wrap source text in a fenced block so chunking fence invariants hold."""
    lowered = (url or "").lower()
    hint = "text"
    for suffix, lang in (
        (".py", "python"),
        (".java", "java"),
        (".js", "javascript"),
        (".ts", "typescript"),
        (".jsx", "jsx"),
        (".tsx", "tsx"),
    ):
        if lowered.endswith(suffix):
            hint = lang
            break
    body = (raw or "").strip()
    if not body:
        return ""
    return f"```{hint}\n{body}\n```"


def classify_content(content_type: str | None, url: str) -> str:
    """Dispatch reader by suffix first, then MIME type: html, markdown, text, source."""
    lowered_url = (url or "").lower().split("?")[0]
    if lowered_url.endswith((".md", ".markdown", ".mdx")):
        return "markdown"
    if lowered_url.endswith((".html", ".htm", ".xhtml")):
        return "html"
    if lowered_url.endswith((".txt", ".rst")):
        return "text"
    if lowered_url.endswith((".py", ".java", ".js", ".ts", ".jsx", ".tsx")):
        return "source"
    ctype = (content_type or "").lower().split(";")[0].strip()
    if ctype in ("text/markdown", "text/x-markdown"):
        return "markdown"
    if ctype == "text/plain":
        return "text"
    if (
        ctype
        in (
            "application/javascript",
            "application/x-python",
            "text/x-python",
            "text/x-java",
            "application/java",
        )
        or "source" in ctype
    ):
        return "source"
    return "html"
