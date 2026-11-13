"""Two-phase Markdown chunking: structural splitting, greedy merging, dual-path budget assembly.

The same batch of structural sections produces one chunk stream per budget path; full-text path
chunks are small and embedding-path chunks are large. Group IDs are frozen once at heading
split boundaries and merging only reuses the first section's ID, so sections from the same
origin share consistent IDs across both paths for alignment.
Overlap is only carried forward with its source noted; over-limit truncation is always counted as a warning.
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
    """Embedding-path entry point for document chunking; returns the embedding stream, matching the embedding stream in the dual path."""
    return split_markdown_dual(md_text, url, budgets=budgets).embedding


def split_markdown_dual(
    md_text: str,
    url: str,
    *,
    budgets: dict[ChunkPath, ChunkBudget] | None = None,
) -> DualChunks:
    """One split produces two chunk streams; twin chunks with identical content are kept only once, on the embedding path."""
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
    return DualChunks(
        bm25=kept_bm25,
        embedding=embedding,
        empty_dropped=bm25_dropped + embed_dropped,
        deduped=len(bm25) - len(kept_bm25),
    )


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
                        _Section(
                            path=cur_path,
                            title=cur_title,
                            content="\n".join(buf).strip(),
                        )
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
        sections.append(
            _Section(path=cur_path, title=cur_title, content="\n".join(buf).strip())
        )
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
                    content_hash=chunk_content_hash(
                        f"{sec.title}\0{sec.path}\0{index_part}"
                    ),
                    keywords=extract_keywords(index_part),
                )
            )
    _assign_relations(chunks, md_source="\n\n".join(s.content for s in merged))
    assert all(_fences_balanced(c.content) for c in chunks), "fence invariant violated"
    return chunks, dropped


def _merge_sections(
    sections: list[_Section], path: ChunkPath, budget: ChunkBudget
) -> list[_Section]:
    """Only sections below the minimum threshold merge with adjacent same-level sections; the absorber records them in merged_from."""
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
    """The only limit-enforcement site on this path: line assembly with fence protection; oversized single lines go through character-budget hard splits."""
    if cost(content, path) <= budget.body_limit:
        return [content]
    parts: list[str] = []
    cur: list[str] = []
    cur_cost = 0
    in_fence = False
    for line in content.splitlines():
        is_fence = bool(_FENCE_RE.match(line))
        line_cost = cost(line, path) + 1
        if (
            cur
            and not is_fence
            and not in_fence
            and cur_cost + line_cost > budget.body_limit
        ):
            parts.append("\n".join(cur))
            cur, cur_cost = [], 0
        if not cur and line_cost > budget.body_limit:
            parts.extend(_split_long_line(line, path, budget.body_limit))
            continue
        if is_fence:
            in_fence = not in_fence
        cur.append(line)
        cur_cost += line_cost
    if cur:
        parts.append("\n".join(cur))
    out: list[str] = []
    for part in parts:
        if cost(part, path) <= budget.body_limit:
            out.append(part)
        else:
            out.extend(_split_lines_fence_aware(part, path, budget.body_limit))
    return [p for p in out if p]


def _embed_head_cost(sec: _Section, path: ChunkPath) -> int:
    """The heading-path head's actual footprint in encoded assembly, conservatively deducted during planning."""
    head = " / ".join(p for p in (sec.title, sec.path) if p)
    if not head:
        return 0
    return cost(head + "\n", path)


def _repair_embed_parts(
    sec: _Section, parts: list[str], budget: ChunkBudget
) -> list[str]:
    """Decoration re-check: if head plus body exceeds the hard limit, tighten the body and re-split to avoid truncation at sync points."""
    if budget.hard_limit <= 0:
        return parts
    head_cost = _embed_head_cost(sec, ChunkPath.EMBEDDING)
    room = budget.hard_limit - head_cost - 1
    room = max(room, 1)
    out: list[str] = []
    for part in parts:
        if cost(part, ChunkPath.EMBEDDING) + head_cost + 1 <= budget.hard_limit:
            out.append(part)
        else:
            out.extend(_split_lines_fence_aware(part, ChunkPath.EMBEDDING, room))
    return [p for p in out if p]


def _overlap_suffix(text: str, path: ChunkPath, limit: int) -> str:
    """Take the tail of the original text under this path's convention; fall back to characters when line accumulation exceeds the limit."""
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
    """Rebuild sort orders and prev/next links after filtering, removing dangling references."""
    for order, chunk in enumerate(chunks):
        chunk.sort_order = order
        chunk.prev_chunk_id = chunks[order - 1].chunk_id if order > 0 else None
        chunk.next_chunk_id = (
            chunks[order + 1].chunk_id if order + 1 < len(chunks) else None
        )
        chunk.overlap_from = None
        chunk.overlap_text = ""


def _attach_prev_overlap(chunks: list[Chunk], budget: ChunkBudget) -> None:
    """Record only the forward predecessor source and metered tail text per this path's convention; no body duplication."""
    if budget.overlap <= 0:
        return
    for prev, cur in itertools.pairwise(chunks):
        cur.overlap_from = prev.chunk_id
        cur.overlap_text = _overlap_suffix(
            prev.content, ChunkPath(cur.chunk_path), budget.overlap
        )


def _fences_balanced(text: str) -> bool:
    count = 0
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            count += 1
    return count % 2 == 0


def _split_lines_fence_aware(text: str, path: ChunkPath, limit: int) -> list[str]:
    """Line assembly that closes and reopens a fence instead of cutting inside it.

    A section may carry one code block longer than the body budget. Slicing it
    verbatim emits chunks whose fences never balance, so the cut becomes a
    synthesized boundary: the open marker closes the current part and opens the
    next one, keeping the block body verbatim.
    """
    if limit <= 0 or not text.strip():
        return [text] if text.strip() else []
    parts: list[str] = []
    cur: list[str] = []
    cur_cost = 0
    marker: str | None = None
    for line in text.splitlines():
        is_fence = bool(_FENCE_RE.match(line))
        line_cost = cost(line, path) + 1
        if cur and not is_fence:
            projected = cur_cost + line_cost
            if marker is not None:
                projected += cost(marker[:3], path) + 1
            if projected > limit:
                if marker is None:
                    parts.append("\n".join(cur))
                    cur, cur_cost = [], 0
                else:
                    parts.append("\n".join([*cur, marker[:3]]))
                    cur = [marker]
                    cur_cost = cost(marker, path) + 1
        if not cur and line_cost > limit:
            parts.extend(split_lines_by_cost(line, path, limit))
            continue
        if is_fence:
            marker = line.strip() if marker is None else None
        cur.append(line)
        cur_cost += line_cost
    if cur:
        parts.append("\n".join(cur))
    return [part for part in parts if part.strip()]


def chunk_content_hash(content: str) -> str:
    """Chunk-level incremental key: content hash; reuse skips re-embedding."""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


_STOPWORDS = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "as",
        "by",
        "at",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "you",
        "we",
        "they",
        "he",
        "she",
        "him",
        "her",
        "them",
        "his",
        "our",
        "your",
        "their",
        "not",
        "no",
        "yes",
        "if",
        "else",
        "when",
        "then",
        "than",
        "so",
        "such",
        "can",
        "will",
        "just",
        "into",
        "over",
        "under",
        "out",
        "up",
        "的",
        "了",
        "在",
        "是",
        "和",
        "与",
        "或",
        "有",
        "个",
        "上",
        "下",
        "中",
        "大",
        "小",
        "不",
        "也",
        "就",
        "都",
        "而",
        "及",
        "等",
        "于",
        "为",
        "来",
        "用",
        "对",
    ]
)


def extract_keywords(text: str, limit: int = 5) -> list[str]:
    """Rule-based keywords: Latin words counted whole with underscore variants added, CJK counted per character."""
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
        if (
            snake
            and snake != token
            and snake not in _STOPWORDS
            and snake not in expanded
            and len(expanded) < limit
        ):
            expanded.append(snake)
    return expanded[:limit]


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;。？！；])\s+")


def _split_long_line(line: str, path: ChunkPath, limit: int) -> list[str]:
    """Oversized single lines split at sentence boundaries first; hard character-budget splits only if still over the limit."""
    pieces = [p for p in _SENTENCE_SPLIT_RE.split(line) if p]
    if len(pieces) > 1 and all(cost(p, path) + 1 <= limit for p in pieces):
        return pieces
    return split_lines_by_cost(line, path, limit)


def _assign_relations(chunks: list[Chunk], *, md_source: str = "") -> None:
    """Fill in prev/next pointers and character ranges; ranges are located in source order, with repeated text advancing the cursor."""
    cursor = 0
    for index, chunk in enumerate(chunks):
        chunk.prev_chunk_id = chunks[index - 1].chunk_id if index > 0 else None
        chunk.next_chunk_id = (
            chunks[index + 1].chunk_id if index + 1 < len(chunks) else None
        )
        if md_source:
            found = md_source.find(chunk.content, cursor)
            if found >= 0:
                chunk.start_char = found
                chunk.end_char = found + len(chunk.content)
                cursor = found + 1


def ensure_hard_limit(text: str, path: ChunkPath, hard_limit: int) -> tuple[str, bool]:
    """Final validation after decoration appends: truncate when over the limit and return whether truncated; the caller logs the warning."""
    if hard_limit == 0 or cost(text, path) <= hard_limit:
        return text, False
    return truncate_to_cost(text, path, hard_limit), True
