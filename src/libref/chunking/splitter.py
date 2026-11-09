"""两阶段 Markdown 分块：结构切分、贪心合并、双路预算装配。

同一批结构节按两路预算各产出一股块流，全文路块小，
向量路块大，分组标识在标题切分处一次冻结，合并只沿用首节标识，
两路同源节标识一致以便对齐。
重叠只向前携带并注明来源，超限截尾一律告警计数。
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import re
from collections import Counter
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
from libref.text.bm25 import clean_text_for_bm25
from libref.text.ident import ident_snake_form

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
    chunk_index: int = 0
    total_chunks: int = 1
    start_char: int = -1
    end_char: int = -1
    prev_chunk_id: str | None = None
    next_chunk_id: str | None = None
    content_hash: str = ""
    keywords: list[str] = field(default_factory=list)
    content_type: str = "text/markdown"
    entities: list[str] = field(default_factory=list)


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
    for idx, sec in enumerate(sections):
        sec.group_id = f"sec_{idx}"
    bm25, bm25_dropped = _split_path(
        sections, url, ChunkPath.BM25, resolved[ChunkPath.BM25]
    )
    embedding, embed_dropped = _split_path(
        sections, url, ChunkPath.EMBEDDING, resolved[ChunkPath.EMBEDDING]
    )
    twin_contents = {c.content for c in embedding}
    kept_bm25 = [c for c in bm25 if c.content not in twin_contents]
    _rechain(embedding)
    _rechain(kept_bm25)
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
    group_id: str = ""


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
    for sec in merged:
        group_id = sec.group_id
        parts = _split_section_by_cost(sec.content, path, budget)
        if path == ChunkPath.EMBEDDING:
            parts = _repair_embed_parts(sec, parts, budget)
        total = len(parts) if parts else 1
        for part_index, part in enumerate(parts):
            if not part.strip():
                dropped += 1
                continue
            index_part = clean_text_for_bm25(part) if path == ChunkPath.BM25 else part
            if not index_part.strip():
                dropped += 1
                continue
            chunks.append(
                Chunk(
                    url=url,
                    path=sec.path,
                    title=sec.title,
                    content=index_part,
                    sort_order=len(chunks),
                    chunk_path=path.value,
                    source_group_id=group_id,
                    chunk_id=f"{group_id}_{path.value}_{part_index}",
                    span_kind=DOCUMENT_RANGE,
                    merged_from=sec.merged_from.copy(),
                    chunk_index=part_index,
                    total_chunks=total,
                    content_hash=chunk_content_hash(f"{sec.title}\0{sec.path}\0{index_part}"),
                    keywords=extract_keywords(index_part),
                )
            )
    _assign_relations(chunks, md_source="\n\n".join(s.content for s in merged))
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
                group_id=prev.group_id,
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
            parts.extend(_split_long_line(line, path, budget.body_limit))
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


def _embed_head_cost(sec: _Section, path: ChunkPath) -> int:
    """标题路径头在编码组装中的实际占位，规划时保守扣除。"""
    head = " / ".join(p for p in (sec.title, sec.path) if p)
    if not head:
        return 0
    return cost(head + "\n", path)


def _repair_embed_parts(sec: _Section, parts: list[str], budget: ChunkBudget) -> list[str]:
    """装饰复检：头加正文超硬限则收紧正文重切，避免同步处截尾。"""
    if budget.hard_limit <= 0:
        return parts
    head_cost = _embed_head_cost(sec, ChunkPath.EMBEDDING)
    room = budget.hard_limit - head_cost - 1
    if room < 1:
        room = 1
    out: list[str] = []
    for part in parts:
        if cost(part, ChunkPath.EMBEDDING) + head_cost + 1 <= budget.hard_limit:
            out.append(part)
        else:
            out.extend(split_lines_by_cost(part, ChunkPath.EMBEDDING, room))
    return [p for p in out if p]


def _overlap_suffix(text: str, path: ChunkPath, limit: int) -> str:
    """按本路口径取尾部原文，行累积超限则按字符兜底。"""
    if limit <= 0 or not text.strip():
        return ""
    lines = text.splitlines()
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        line_cost = cost(line, path) + 1
        if kept and used + line_cost > limit:
            break
        if not kept and line_cost > limit:
            return truncate_to_cost(line, path, limit)
        kept.append(line)
        used += line_cost
    kept.reverse()
    return "\n".join(kept).strip()


def _rechain(chunks: list[Chunk]) -> None:
    """过滤后重建序号与前后链，消除悬指。"""
    for order, chunk in enumerate(chunks):
        chunk.sort_order = order
        chunk.prev_chunk_id = chunks[order - 1].chunk_id if order > 0 else None
        chunk.next_chunk_id = chunks[order + 1].chunk_id if order + 1 < len(chunks) else None
        chunk.overlap_from = None
        chunk.overlap_text = ""


def _attach_prev_overlap(
    chunks: list[Chunk], budget: ChunkBudget
) -> None:
    """只向前记录前驱来源与按口径计量的尾部实文，正文不重复。"""
    if budget.overlap <= 0:
        return
    for prev, cur in itertools.pairwise(chunks):
        cur.overlap_from = prev.chunk_id
        cur.overlap_text = _overlap_suffix(prev.content, ChunkPath(cur.chunk_path), budget.overlap)


def _fences_balanced(text: str) -> bool:
    count = 0
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            count += 1
    return count % 2 == 0


def chunk_content_hash(content: str) -> str:
    """块级增量键：内容哈希，复用即跳过向量重编码。"""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


_STOPWORDS = frozenset(
    "the a an and or of to in on for with as by at from is are was were be been "
    "this that these those it its you we they he she him her them his our your their "
    "not no yes if else when then than so such can will just into over under out up "
    "的 了 在 是 和 与 或 有 个 上 下 中 大 小 不 也 就 都 而 及 等 于 为 来 用 对".split()
)


def extract_keywords(text: str, limit: int = 5) -> list[str]:
    """规则关键词：拉丁词整体计并补下划线变体，中文按字计。"""
    if not text or not text.strip():
        return []
    latin = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text)
    cjk = re.findall(r"[\u4e00-\u9fff]{2,4}", text)
    counts: Counter[str] = Counter()
    for token in [t.lower() for t in latin] + cjk:
        if token in _STOPWORDS:
            continue
        counts[token] += 1
    ranked = [token for token, _ in counts.most_common(limit)]
    expanded: list[str] = []
    for token in ranked:
        if token not in expanded:
            expanded.append(token)
        snake = ident_snake_form(token)
        if snake and snake != token and snake not in _STOPWORDS and snake not in expanded:
            if len(expanded) < limit:
                expanded.append(snake)
    return expanded[:limit]


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;。？！；])\s+")


def _split_long_line(line: str, path: ChunkPath, limit: int) -> list[str]:
    """超长单行优先按句子边界拆，仍超限才按字符预算硬切。"""
    pieces = [p for p in _SENTENCE_SPLIT_RE.split(line) if p]
    if len(pieces) > 1 and all(cost(p, path) + 1 <= limit for p in pieces):
        return pieces
    return split_lines_by_cost(line, path, limit)


def _assign_relations(chunks: list[Chunk], *, md_source: str = "") -> None:
    """补前后指针与字符区间，区间按源文顺序查找，重复文本顺序后移。"""
    cursor = 0
    for index, chunk in enumerate(chunks):
        chunk.prev_chunk_id = chunks[index - 1].chunk_id if index > 0 else None
        chunk.next_chunk_id = chunks[index + 1].chunk_id if index + 1 < len(chunks) else None
        if md_source:
            found = md_source.find(chunk.content, cursor)
            if found >= 0:
                chunk.start_char = found
                chunk.end_char = found + len(chunk.content)
                cursor = found + 1


def ensure_hard_limit(
    text: str, path: ChunkPath, hard_limit: int
) -> tuple[str, bool]:
    """装饰追加后的最终校验，超限截尾并返回是否截断，告警由调用方打。"""
    if hard_limit == 0 or cost(text, path) <= hard_limit:
        return text, False
    return truncate_to_cost(text, path, hard_limit), True
