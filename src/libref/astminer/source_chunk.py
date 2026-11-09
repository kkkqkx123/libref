"""源码分块组装：实体原文直接作块文本，零模型调用。

成员源码即块内容，两路按各自预算切分，共享源分组标识。
空区间在源头跳过并计数；无成员的组以组区间作块；
有独立文档说明的成员不参与合并。
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field

from libref.chunking.budget import (
    ENCLOSING_ENTITY,
    EXACT_ENTITIES,
    GROUP_FALLBACK,
    ChunkBudget,
    ChunkPath,
    cost,
    default_budgets,
    split_lines_by_cost,
)

logger = logging.getLogger(__name__)


@dataclass
class SourceChunk:
    file: str
    repo: str
    qual: str | None
    group_id: str
    chunk_id: str
    chunk_path: str
    content: str
    start_byte: int
    end_byte: int
    span_kind: str
    merged_quals: list[str] = field(default_factory=list)
    overlap_from: str | None = None
    overlap_text: str = ""


@dataclass
class SourceSplit:
    chunks: list[SourceChunk]
    empty_dropped: int = 0


def _header_for_member(qual: str | None, decls_by_qual: dict, file: str) -> str:
    """给向量路配口语头，限定名加种类加文件加签名。"""
    if not qual:
        return ""
    decl = (decls_by_qual or {}).get(qual)
    if decl is None:
        return f"{qual} in {file}" if file else str(qual)
    kind = str(getattr(decl, "kind", "") or "")
    sig = str(getattr(decl, "signature", "") or "")
    head = qual
    if kind:
        head = f"{head} {kind}"
    if file:
        head = f"{head} in {file}"
    if sig.strip():
        one = " ".join(sig.split())
        if len(one) > 300:
            one = one[:300]
        return f"{head}\nSignature: {one}"
    return head


def build_source_chunks(
    groups,
    decls_by_qual: dict,
    source: bytes,
    path: str,
    *,
    budgets: dict[ChunkPath, ChunkBudget] | None = None,
    repo: str = "",
) -> SourceSplit:
    """按成员边界组装源码块，返回块流与空块计数。"""
    resolved = budgets or default_budgets()
    chunks: list[SourceChunk] = []
    dropped = 0
    for group in groups:
        if group.header_context.strip():
            _emit_header_context(
                group, source, resolved, chunks, repo
            )
        members = [q for q in group.member_quals if q in group.member_spans]
        if not members:
            text = _slice(source, *group.header_span)
            if not text.strip():
                dropped += 1
                continue
            for chunk_path in ChunkPath:
                budget = resolved[chunk_path]
                dropped += _emit_pieces(
                    group, None, text, group.header_span, chunk_path,
                    budget, EXACT_ENTITIES, chunks, source, repo,
                )
            continue
        for qual in members:
            span = group.member_spans[qual]
            text = _slice(source, *span)
            if not text.strip():
                dropped += 1
                continue
            for chunk_path in ChunkPath:
                budget = resolved[chunk_path]
                before = len(chunks)
                dropped += _emit_pieces(
                    group, qual, text, span, chunk_path,
                    budget, EXACT_ENTITIES, chunks, source, repo,
                )
                if chunk_path == ChunkPath.EMBEDDING:
                    head = _header_for_member(qual, decls_by_qual, path)
                    if head:
                        for fresh in chunks[before:]:
                            if fresh.qual == qual and fresh.chunk_path == chunk_path.value:
                                fresh.content = f"{head}\n{fresh.content}"
                                break
    _merge_undersized(groups, resolved, chunks)
    _attach_prev_overlap(chunks, resolved)
    return SourceSplit(chunks=chunks, empty_dropped=dropped)


def _slice(source: bytes, start: int, end: int) -> str:
    return source[start:end].decode("utf-8", "replace")


def _emit_header_context(group, source: bytes, budgets, chunks: list[SourceChunk], repo: str) -> None:
    text = group.header_context.strip()
    if not text:
        return
    first_start = min(
        [group.header_span[0]]
        + [span[0] for span in group.member_spans.values()]
    )
    for chunk_path in ChunkPath:
        budget = budgets[chunk_path]
        pieces = split_lines_by_cost(text, chunk_path, budget.body_limit)
        for index, piece in enumerate(pieces):
            if not piece.strip():
                continue
            chunks.append(
                SourceChunk(
                    file=group.file,
                    repo=repo,
                    qual=None,
                    group_id=group.group_id,
                    chunk_id=f"{group.group_id}::header::{chunk_path.value}::{index}",
                    chunk_path=chunk_path.value,
                    content=piece,
                    start_byte=0,
                    end_byte=first_start,
                    span_kind=GROUP_FALLBACK,
                )
            )


def _emit_pieces(
    group, qual: str | None, text: str, span: tuple[int, int],
    chunk_path: ChunkPath, budget: ChunkBudget, span_kind: str,
    chunks: list[SourceChunk], source: bytes, repo: str,
) -> int:
    dropped = 0
    start, end = span
    located: list[tuple[str, str, int, int]] = []
    if cost(text, chunk_path) <= budget.body_limit:
        located = [(text, span_kind, start, end)]
    else:
        cursor = 0
        for part in split_lines_by_cost(text, chunk_path, budget.body_limit):
            at = text.find(part, cursor)
            if at < 0:
                at = cursor
            piece_start = start + len(text[:at].encode("utf-8"))
            piece_end = piece_start + len(part.encode("utf-8"))
            located.append((part, ENCLOSING_ENTITY, piece_start, piece_end))
            cursor = at + len(part)
    for index, (piece, kind, piece_start, piece_end) in enumerate(located):
        if not piece.strip():
            dropped += 1
            continue
        chunks.append(
            SourceChunk(
                file=group.file,
                repo=repo,
                qual=qual,
                group_id=group.group_id,
                chunk_id=f"{group.group_id}::{chunk_path.value}::{index}",
                chunk_path=chunk_path.value,
                content=piece,
                start_byte=piece_start,
                end_byte=piece_end,
                span_kind=kind,
            )
        )
    return dropped


def _merge_undersized(groups, budgets, chunks: list[SourceChunk]) -> None:
    """低于最小阈值的非自包含块向相邻同组块合并，保留左块身份。"""
    by_group: dict[tuple[str, str], list[int]] = {}
    for pos, chunk in enumerate(chunks):
        by_group.setdefault((chunk.group_id, chunk.chunk_path), []).append(pos)
    doc_of = {g.group_id: dict(g.doc_members) for g in groups}
    condemned: set[int] = set()
    for (group_id, _), positions in by_group.items():
        docs = doc_of.get(group_id, {})
        for left_pos, right_pos in itertools.pairwise(positions):
            left, right = chunks[left_pos], chunks[right_pos]
            if left_pos in condemned or right_pos in condemned:
                continue
            budget = budgets[ChunkPath(left.chunk_path)]
            if (
                cost(left.content, ChunkPath(left.chunk_path)) < budget.min_limit
                and not docs.get(left.qual or "", False)
                and not docs.get(right.qual or "", False)
                and cost(left.content + "\n" + right.content,
                         ChunkPath(left.chunk_path)) <= budget.body_limit
            ):
                left.content = left.content + "\n" + right.content
                left.end_byte = right.end_byte
                if right.qual and right.qual != left.qual:
                    left.merged_quals.append(right.qual)
                condemned.add(right_pos)
    for pos in sorted(condemned, reverse=True):
        del chunks[pos]


def _attach_prev_overlap(chunks: list[SourceChunk], budgets) -> None:
    by_path: dict[str, list[SourceChunk]] = {}
    for chunk in chunks:
        by_path.setdefault(chunk.chunk_path, []).append(chunk)
    for path_value, stream in by_path.items():
        budget = budgets[ChunkPath(path_value)]
        if budget.overlap <= 0:
            continue
        for prev, cur in itertools.pairwise(stream):
            tail = _overlap_tail(prev.content, ChunkPath(path_value), budget.overlap)
            if tail.strip():
                cur.overlap_from = prev.chunk_id
                cur.overlap_text = tail


def _overlap_tail(content: str, path: ChunkPath, overlap: int) -> str:
    kept: list[str] = []
    total = 0
    for line in reversed(content.splitlines()):
        line_cost = cost(line, path) + 1
        if kept and total + line_cost > overlap:
            break
        kept.append(line)
        total += line_cost
    return "\n".join(reversed(kept))


def as_chunk(source_chunk: SourceChunk, url: str):
    """转文档块形态以复用落库与向量组装，溯源字段完整保留。"""
    from libref.chunking.splitter import Chunk, ident_snake_form

    names: list[str] = []
    qual = source_chunk.qual or ""
    if qual:
        for cand in (
            qual.lower(),
            ident_snake_form(qual),
            qual.rsplit(".", 1)[-1].lower(),
            ident_snake_form(qual.rsplit(".", 1)[-1]),
        ):
            if cand and len(cand) >= 2 and not cand[0].isdigit() and cand not in names:
                names.append(cand)
    return Chunk(
        url=url,
        path=source_chunk.file,
        title=source_chunk.qual or source_chunk.file,
        content=source_chunk.content,
        keywords=names[:5],
        sort_order=0,
        chunk_path=source_chunk.chunk_path,
        source_group_id=source_chunk.group_id,
        chunk_id=source_chunk.chunk_id,
        span_kind=source_chunk.span_kind,
        merged_from=list(source_chunk.merged_quals),
        overlap_from=source_chunk.overlap_from,
        overlap_text=source_chunk.overlap_text,
    )
