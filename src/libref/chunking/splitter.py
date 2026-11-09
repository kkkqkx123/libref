"""两阶段 Markdown 分块：结构切分、贪心合并、双路预算装配。

同一批结构节按两路预算各产出一股块流，全文路块小，
向量路块大，两路共享源分组标识以便对齐。
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
    start_char: int = -1
    end_char: int = -1
    prev_chunk_id: str | None = None
    next_chunk_id: str | None = None
    content_hash: str = ""
    keywords: list[str] = field(default_factory=list)
    content_type: str = "text/markdown"


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
                    content_hash=chunk_content_hash(part),
                    keywords=extract_keywords(part),
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
    """规则关键词：拉丁词整体计、中文按字计，去停用词后按频次取前列。"""
    if not text or not text.strip():
        return []
    latin = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text.lower())
    cjk = re.findall(r"[\u4e00-\u9fff]{2,4}", text)
    counts: Counter[str] = Counter()
    for token in latin + cjk:
        if token in _STOPWORDS:
            continue
        counts[token] += 1
    return [token for token, _ in counts.most_common(limit)]


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
