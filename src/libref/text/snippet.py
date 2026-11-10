"""Snippet and material truncation with concise boundary markers."""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"^(```|~~~)")


def xml_escape_attr(value: object) -> str:
    """Escape a value for use inside a double-quoted XML attribute."""
    text = "" if value is None else str(value)
    out: list[str] = []
    for ch in text:
        if ch == "&":
            out.append("&amp;")
        elif ch == "<":
            out.append("&lt;")
        elif ch == ">":
            out.append("&gt;")
        elif ch == '"':
            out.append("&quot;")
        elif ch == "'":
            out.append("&apos;")
        else:
            out.append(ch)
    return "".join(out)


def gap_tag(lines: int | None = None, span: str | None = None) -> str:
    """Self-closing marker for omitted lines between rendered segments."""
    if lines is None and not span:
        return "<gap/>"
    parts = ""
    if lines is not None:
        parts += f' lines="{int(lines)}"'
    if span:
        parts += f' range="{xml_escape_attr(span)}"'
    return f"<gap{parts}/>"


def reference_tag(
    path: str, lines: str, reason: str, tokens: int | None = None
) -> str:
    """Self-closing pointer replacing a body dropped for budget or freshness."""
    base = (
        f'<reference path="{xml_escape_attr(path)}" '
        f'lines="{xml_escape_attr(lines)}" '
        f'reason="{xml_escape_attr(reason)}"'
    )
    if tokens:
        base += f' tokens="~{int(tokens)}"'
    return base + "/>"


def omitted_tag(units: int, tokens: int | None = None) -> str:
    """Tail marker naming how many whole units were dropped for budget."""
    if tokens:
        return f'<omitted units="{int(units)}" tokens="~{int(tokens)}"/>'
    return f'<omitted units="{int(units)}"/>'


def unit_tag(attrs: dict[str, object], body: str) -> str:
    """Wrap a verbatim body with a self-locating unit tag."""
    rendered = "".join(
        f' {key}="{xml_escape_attr(value)}"' for key, value in attrs.items()
    )
    return f"<unit{rendered}>\n{body}\n</unit>"


def fences_balanced(text: str) -> bool:
    """Whether fenced code blocks open and close in pairs."""
    count = 0
    for line in (text or "").splitlines():
        if _FENCE_RE.match(line):
            count += 1
    return count % 2 == 0


def _fence_aware_cut(text: str, limit: int) -> str:
    """Cut at a line boundary then backtrack until fences balance."""
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    cut = body.rfind("\n", 0, limit)
    end = cut if cut > limit // 2 else limit
    candidate = body[:end].rstrip()
    while candidate and not fences_balanced(candidate):
        fallback = candidate.rfind("\n")
        if fallback < 0:
            break
        candidate = candidate[:fallback].rstrip()
        if not candidate:
            break
    return candidate


def make_snippet(content: str | None, query: str, *, radius: int, cap: int) -> str:
    """Cut a context window around the first query hit and mark hits with tags."""
    text = (content or "").strip()
    if not text:
        return "<excerpt/>"
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
    prefix = gap_tag() if start > 0 else ""
    suffix = gap_tag() if end < len(text) else ""
    window = text[start:end]
    for token in sorted(set(re.findall(r"[\w-]+", query)), key=len, reverse=True):
        if not token:
            continue
        pattern = re.compile(re.escape(token), re.IGNORECASE)
        window = pattern.sub(lambda m: f"<hit>{m.group(0)}</hit>", window)
    return f"<excerpt>{prefix}{window}{suffix}</excerpt>"


def truncate_material(text: str | None, limit: int) -> str:
    """Truncate long answer material on a fence-aware line boundary with a gap marker."""
    body = (text or "").strip()
    if not body:
        return ""
    if len(body) <= limit:
        return body
    candidate = _fence_aware_cut(body, limit)
    if not candidate:
        return gap_tag()
    return candidate + "\n" + gap_tag()
