"""Scrape planning: budgets, target selection, outcome counts, chunk shaping."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from libref.chunking import ChunkPath
from libref.chunking.budget import ChunkBudget, budgets_from_settings
from libref.crawler import build_scope
from libref.crawler.scope import (
    BudgetExceeded,
    crawl_key,
    score_url,
    sort_by_relevance,
)

__all__ = ["BudgetExceeded"]
from libref.store.document_store import DocumentInput
from libref.text.normalize import normalize_markdown

PIPELINE_STEPS = ("discover", "fetch", "clean", "split", "store", "vectors")


@dataclass
class FetchDetail:
    """Per-page follow-up data: followable out-links plus the canonical alias."""

    links: list[str] = field(default_factory=list)
    canonical: str = ""
    structure_links: list[str] = field(default_factory=list)


def doc_budgets(settings) -> dict[ChunkPath, ChunkBudget]:
    """Build the two-path document chunking budgets from settings; the single assembly point for chunk budgets."""
    return budgets_from_settings(settings)


def budget_fingerprint(budgets: dict[ChunkPath, ChunkBudget]) -> str:
    """Chunk budget fingerprint; after a budget change old cleaning-cache entries no longer hit."""
    parts = []
    for path in sorted(budgets, key=lambda p: p.value):
        budget = budgets[path]
        parts.append(
            f"{path.value}:{budget.body_limit}:{budget.min_limit}:{budget.overlap}"
        )
    return "|".join(parts)


def normalize_text_content(raw: str) -> str:
    """Plain text and Markdown normalization without HTML parsing."""
    return normalize_markdown(raw)


def plan_targets(
    seed, start_url: str, scope: dict[str, Any] | None, max_pages: int | None
):
    """Scrape target planning: scope filter plus start-page fallback plus pagination truncation; pure decisions, no DB access."""
    policy = build_scope(start_url, scope)
    extra = [u for u in ((scope or {}).get("urls") or []) if isinstance(u, str) and u]
    candidates = (
        list(extra) + seed.urls + [u for u in seed.cross_site if u not in seed.urls]
    )
    accepted, rejected, stats = policy.filter_with_stats(candidates)
    accepted = list(dict.fromkeys(crawl_key(url) for url in accepted))
    if not accepted and policy.accept(start_url):
        accepted = [start_url]
    query = (scope or {}).get("crawl_query")
    if query and query.strip():
        threshold = (scope or {}).get("score_threshold")
        if threshold is not None:
            bound = float(threshold)
            accepted = [u for u in accepted if score_url(u, query) >= bound]
        accepted = sort_by_relevance(accepted, query)
    if max_pages is not None and len(accepted) > max_pages:
        raise BudgetExceeded(
            f"ingest budget exceeded: {len(accepted)} candidates over budget {max_pages}"
        )
    return accepted, rejected, bool(seed.truncated), stats


def record_outcome(
    counts, removed_all, inserted_all, result, empty_dropped, render_skipped, stage_ms
) -> None:
    """Per-page outcome summary: counts, vector candidates, stage timings; one funnel."""
    counts[result.outcome] = counts.get(result.outcome, 0) + 1
    counts["chunk_empty_dropped"] += empty_dropped
    if render_skipped:
        counts["render_skipped"] += 1
    counts["chunk_reused"] += len(getattr(result, "reused_ids", []))
    counts["snapshot_written"] += int(stage_ms.get("snapshot_written", 0))
    counts["snapshot_quota_skipped"] += int(stage_ms.get("snapshot_quota_skipped", 0))
    for step in PIPELINE_STEPS:
        counts[f"{step}_ms"] += int(stage_ms.get(f"{step}_ms", 0))
    removed_all.extend(result.removed_ids)
    inserted_all.extend(result.inserted_ids)


def build_doc_inputs(
    raw_chunks, content_type: str | None, kind: str
) -> list[DocumentInput]:
    """Document chunk input assembly: sort order and type backfill; the single shaping point before storing."""
    for order, chunk in enumerate(raw_chunks):
        chunk.sort_order = order
        if (
            not getattr(chunk, "content_type", "")
            or chunk.content_type == "text/markdown"
        ):
            chunk.content_type = (content_type or "text/markdown").split(";")[
                0
            ].strip() or kind
    return [
        DocumentInput(
            url=c.url,
            path=c.path,
            title=c.title,
            content=c.content,
            sort_order=c.sort_order,
            chunk_path=getattr(c, "chunk_path", "embedding"),
            source_group_id=getattr(c, "source_group_id", ""),
            chunk_id=getattr(c, "chunk_id", ""),
            span_kind=getattr(c, "span_kind", "document_range"),
            merged_from=list(getattr(c, "merged_from", []) or []),
            overlap_from=getattr(c, "overlap_from", None),
            overlap_text=str(getattr(c, "overlap_text", "") or ""),
            chunk_index=int(getattr(c, "chunk_index", 0) or 0),
            total_chunks=max(1, int(getattr(c, "total_chunks", 1) or 1)),
            content_hash=getattr(c, "content_hash", ""),
            keywords=list(getattr(c, "keywords", []) or []),
            content_type=getattr(c, "content_type", "text/markdown"),
            start_char=getattr(c, "start_char", -1),
            end_char=getattr(c, "end_char", -1),
            prev_chunk_id=getattr(c, "prev_chunk_id", None),
            next_chunk_id=getattr(c, "next_chunk_id", None),
        )
        for c in raw_chunks
    ]
