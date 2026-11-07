"""两阶段 Markdown 分块。

蓝本 docs-mcp-server SemanticMarkdownSplitter / GreedySplitter 模式（结构感知切分 +
贪心合并）。定制点：代码围栏内 `#` 不视为标题（围栏状态机），超长块硬切时保证
围栏平衡不变量——输出块内 ``` 必须成对。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^(```|~~~)")


@dataclass
class Chunk:
    url: str
    path: str      # 标题路径，"/" 连接，如 "Guide / Install"
    title: str     # 末级标题
    content: str
    sort_order: int


def split_markdown(
    md_text: str,
    url: str,
    *,
    target_chars: int = 1600,
    min_chars: int = 200,
) -> list[Chunk]:
    """两阶段分块：结构切分 → 贪心合并 → 围栏平衡硬切。"""
    sections = _split_by_headings(md_text)
    sections = [s for s in sections if s.content.strip()]
    sections = _greedy_merge(sections, target_chars=target_chars, min_chars=min_chars)
    chunks: list[Chunk] = []
    for order, sec in enumerate(sections):
        for part in _split_long(sec.content, target_chars):
            chunks.append(
                Chunk(url=url, path=sec.path, title=sec.title, content=part, sort_order=order)
            )
    for i, chunk in enumerate(chunks):
        chunk.sort_order = i
    assert all(_fences_balanced(c.content) for c in chunks), "fence invariant violated"
    return chunks


@dataclass
class _Section:
    path: str
    title: str
    content: str


def _split_by_headings(md_text: str) -> list[_Section]:
    sections: list[_Section] = []
    path: dict[int, str] = {}  # level -> title
    buf: list[str] = []
    cur_path = ""
    cur_title = ""
    in_fence = False

    for line in md_text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            buf.append(line)
            continue
        if not in_fence:
            m = _HEADING_RE.match(line)
            if m:
                if buf:
                    sections.append(
                        _Section(path=cur_path, title=cur_title, content="\n".join(buf).strip())
                    )
                    buf = []
                level = len(m.group(1))
                title = m.group(2).strip()
                path[level] = title
                for deeper in [k for k in path if k > level]:
                    del path[deeper]
                cur_title = title
                cur_path = " / ".join(path[k] for k in sorted(path))
                buf.append(line)
                continue
        buf.append(line)

    if buf:
        sections.append(_Section(path=cur_path, title=cur_title, content="\n".join(buf).strip()))
    return sections


def _greedy_merge(sections: list[_Section], *, target_chars: int, min_chars: int) -> list[_Section]:
    """过小的相邻同级块（相同标题路径）向后合并至 target 附近。"""
    merged: list[_Section] = []
    for sec in sections:
        if (
            merged
            and merged[-1].path == sec.path
            and len(merged[-1].content) + len(sec.content) <= target_chars * 2
        ):
            prev = merged[-1]
            merged[-1] = _Section(path=prev.path, title=prev.title,
                                  content=prev.content + "\n\n" + sec.content)
        else:
            merged.append(sec)
    return merged


def _split_long(content: str, target_chars: int) -> list[str]:
    if len(content) <= target_chars * 1.5:
        return [content]
    parts: list[str] = []
    cur: list[str] = []
    cur_len = 0
    in_fence = False
    for line in content.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        line_len = len(line) + 1
        # 切点仅在围栏外且当前块已达目标长度
        if cur_len + line_len > target_chars and not in_fence and cur:
            parts.append("\n".join(cur).strip())
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += line_len
    if cur:
        parts.append("\n".join(cur).strip())
    return [p for p in parts if p]


def _fences_balanced(text: str) -> bool:
    count = 0
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            count += 1
    return count % 2 == 0
