"""两阶段 Markdown 分块：结构切分、贪心合并、双路预算装配。

同一批结构节按两路预算各产出一股块流，全文路块小，
向量路块大，两路共享源分组标识以便对齐。
重叠只向前携带并注明来源，超限截尾一律告警计数。
"""

from __future__ import annotations

import itertools
import logging
import re
from dataclasses import dataclass, field

from libref.chunking.budget import (
    DOCUMENT_RANGE,
    ChunkBudget,
    ChunkPath,
    cost,
    default_budgets,
    split_lines_by_cost,
    truncate_to_cost,
)

logger = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^(```|~~~)")


@dataclass
class Chunk:
    url: str
    path: str
    title: str
    content: str
    sort_order: int
    chunk_path: str = "embedding"
    source_group_id: str = ""
    chunk_id: str = ""
    span_kind: str = DOCUMENT_RANGE
    merged_from: list[str] = field(default_factory=list)
    overlap_from: str | None = None
    overlap_text: str = ""


@dataclass
class DualChunks:
    bm25: list[Chunk]
    embedding: list[Chunk]
    empty_dropped: int = 0
    deduped: int = 0


def split_markdown(
    md_text: str,
    url: str,
    *,
    budgets: dict[ChunkPath, ChunkBudget] | None = None,
) -> list[Chunk]:
    """文档分块的向量路入口，返回嵌入流，行为与双路中的嵌入流一致。"""
    return split_markdown_dual(md_text, url, budgets=budgets).embedding


def split_markdown_dual(
    md_text: str,
    url: str,
    *,
    budgets: dict[ChunkPath, ChunkBudget] | None = None,
) -> DualChunks:
    """一次切分产出两股块流，内容相同的孪生块只保留向量路一份。"""
    resolved = budgets or default_budgets()
    sections = [s for s in _split_by_headings(md_text) if s.content.strip()]
    bm25, bm25_dropped = _split_path(
        sections, url, ChunkPath.BM25, resolved[ChunkPath.BM25]
    )
    embedding, embed_dropped = _split_path(
        sections, url, ChunkPath.EMBEDDING, resolved[ChunkPath.EMBEDDING]
    )
    twin_contents = {c.content for c in embedding}
    kept_bm25 = [c for c in bm25 if c.content not in twin_contents]
    for order, chunk in enumerate(embedding):
        chunk.sort_order = order
    for order, chunk in enumerate(kept_bm25):
        chunk.sort_order = order
    _attach_prev_overlap(kept_bm25, resolved[ChunkPath.BM25])
    _attach_prev_overlap(embedding, resolved[ChunkPath.EMBEDDING])
    return DualChunks(bm25=kept_bm25, embedding=embedding,
                      empty_dropped=bm25_dropped + embed_dropped,
                      deduped=len(bm25) - len(kept_bm25))


@dataclass
class _Section:
    path: str
    title: str
    content: str
    merged_from: list[str] = field(default_factory=list)


def _split_by_headings(md_text: str) -> list[_Section]:
    sections: list[_Section] = []
    path: dict[int, str] = {}
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
                        _Section(path=cur_path, title=cur_title,
                                 content="\n".join(buf).strip())
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
        sections.append(_Section(path=cur_path, title=cur_title,
                                 content="\n".join(buf).strip()))
    for sec in sections:
        sec.merged_from = [sec.title or sec.path]
    return sections


def _split_path(
    sections: list[_Section],
    url: str,
    path: ChunkPath,
    budget: ChunkBudget,
) -> tuple[list[Chunk], int]:
    merged = _merge_sections(sections, path, budget)
    chunks: list[Chunk] = []
    dropped = 0
    for group_index, sec in enumerate(merged):
        group_id = f"sec_{group_index}"
        for part_index, part in enumerate(
            _split_section_by_cost(sec.content, path, budget)
        ):
            if not part.strip():
                dropped += 1
                continue
            chunks.append(
                Chunk(
                    url=url,
                    path=sec.path,
                    title=sec.title,
                    content=part,
                    sort_order=len(chunks),
                    chunk_path=path.value,
                    source_group_id=group_id,
                    chunk_id=f"{group_id}_{path.value}_{part_index}",
                    span_kind=DOCUMENT_RANGE,
                    merged_from=sec.merged_from.copy(),
                )
            )
    assert all(_fences_balanced(c.content) for c in chunks), "fence invariant violated"
    return chunks, dropped


def _merge_sections(
    sections: list[_Section], path: ChunkPath, budget: ChunkBudget
) -> list[_Section]:
    """低于最小阈值的节才与相邻同级节合并，合并者记入被并集合。"""
    merged: list[_Section] = []
    for sec in sections:
        if (
            merged
            and merged[-1].path == sec.path
            and cost(merged[-1].content, path) < budget.min_limit
            and cost(merged[-1].content + "\n\n" + sec.content, path)
            <= budget.body_limit
        ):
            prev = merged[-1]
            merged[-1] = _Section(
                path=prev.path,
                title=prev.title,
                content=prev.content + "\n\n" + sec.content,
                merged_from=prev.merged_from + sec.merged_from,
            )
        else:
            merged.append(sec)
    return merged


def _split_section_by_cost(
    content: str, path: ChunkPath, budget: ChunkBudget
) -> list[str]:
    """本路唯一的上限执行位置：行装配加围栏保护，单行超限走字符预算硬切。"""
    if cost(content, path) <= budget.body_limit:
        return [content]
    parts: list[str] = []
    cur: list[str] = []
    cur_cost = 0
    in_fence = False
    for line in content.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        line_cost = cost(line, path) + 1
        if cur and cur_cost + line_cost > budget.body_limit and not in_fence:
            parts.append("\n".join(cur))
            cur, cur_cost = [], 0
        if not cur and line_cost > budget.body_limit:
            parts.extend(split_lines_by_cost(line, path, budget.body_limit))
            continue
        cur.append(line)
        cur_cost += line_cost
    if cur:
        parts.append("\n".join(cur))
    out: list[str] = []
    for part in parts:
        if cost(part, path) <= budget.body_limit:
            out.append(part)
        else:
            out.extend(split_lines_by_cost(part, path, budget.body_limit))
    return [p for p in out if p]


def _attach_prev_overlap(
    chunks: list[Chunk], budget: ChunkBudget
) -> None:
    """只向前携带重叠，重叠区注明来源块，不在正文内重复。"""
    if budget.overlap <= 0:
        return
    for prev, cur in itertools.pairwise(chunks):
        tail = _overlap_tail(prev.content, cur.chunk_path, budget.overlap)
        if tail.strip():
            cur.overlap_from = prev.chunk_id
            cur.overlap_text = tail


def _overlap_tail(content: str, chunk_path: str, overlap: int) -> str:
    path = ChunkPath(chunk_path) if chunk_path in ("bm25", "embedding") else None
    if path is None:
        return ""
    lines = content.splitlines()
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        line_cost = cost(line, path) + 1
        if kept and total + line_cost > overlap:
            break
        kept.append(line)
        total += line_cost
    return "\n".join(reversed(kept))


def _fences_balanced(text: str) -> bool:
    count = 0
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            count += 1
    return count % 2 == 0


def ensure_hard_limit(
    text: str, path: ChunkPath, hard_limit: int
) -> tuple[str, bool]:
    """装饰追加后的最终校验，超限截尾并返回是否截断，告警由调用方打。"""
    if hard_limit == 0 or cost(text, path) <= hard_limit:
        return text, False
    return truncate_to_cost(text, path, hard_limit), True
