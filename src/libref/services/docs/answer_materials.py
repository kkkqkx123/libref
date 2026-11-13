"""Answer materials: grounded QA prompts, hard material budgeting, and boundary-marked units."""

from __future__ import annotations

import re
from typing import Any

from libref.text.snippet import omitted_tag, truncate_material, unit_tag

SYSTEM_PROMPT = (
    "Answer only using the provided materials. "
    "If the materials are insufficient, say you do not know. "
    "Cite sources using material numbers like [1], [2]; numbered materials are the only citable sources. "
    "Pattern hints are mined usage hints, not sources, and must never be cited. "
    "Reply in the same language as the user query. "
    "When you refuse for lack of evidence, say what is missing "
    "and suggest which library or version to index next."
)

_CITE_RE = re.compile(r"\[(\d+)\]")


def material_block(index: int, item: dict[str, Any], body: str) -> str:
    """Render one numbered material as a self-locating unit with verbatim body."""
    doc = item.get("id")
    page_id = item.get("page_id")
    order = item.get("sort_order")
    attrs = {
        "n": str(index),
        "url": str(item.get("url") or ""),
        "title": str(item.get("title") or item.get("url") or ""),
        "doc": "" if doc is None else str(doc),
        "chunk": str(item.get("chunk_id", "") or ""),
        "group": str(item.get("source_group_id", "") or ""),
        "page": "" if page_id is None else str(page_id),
        "order": "" if order is None else str(order),
    }
    return unit_tag(attrs, body)


def build_pattern_block(labels: list[dict[str, Any]], limit: int) -> str:
    """Render mined pattern labels as non-citable hints outside the materials block."""
    if not labels:
        return ""
    units: list[str] = []
    for label in labels[: max(1, int(limit))]:
        pattern = label.get("pattern") or []
        body = str(label.get("summary") or "")
        workflow = str(label.get("workflow") or "")
        if workflow:
            body = f"{body} Workflow: {workflow}" if body else workflow
        attrs = {
            "kind": "pattern-hint",
            "name": str(label.get("name") or ""),
            "pattern": ", ".join(str(p) for p in pattern),
        }
        units.append(unit_tag(attrs, truncate_material(body, limit)))
    return (
        f'<pattern_hints count="{len(units)}">\n'
        + "\n".join(units)
        + "\n</pattern_hints>"
    )


def apply_total_budget(
    results: list[dict[str, Any]],
    full_by_id: dict[int, str],
    per_limit: int,
    total_limit: int,
) -> tuple[list[dict[str, Any]], dict[int, str], int]:
    """Fit materials into the total budget in rank order; unreadable or overflowing tail items drop out."""
    kept: list[dict[str, Any]] = []
    bodies: dict[int, str] = {}
    used = 0
    for item in results:
        doc_id = int(item["id"])
        body = truncate_material(full_by_id.get(doc_id), per_limit)
        if not body:
            continue
        remaining = total_limit - used
        if remaining <= 0:
            break
        if len(body) > remaining:
            body = truncate_material(body, remaining)
        if not body:
            break
        kept.append(item)
        bodies[doc_id] = body
        used += len(body)
    return kept, bodies, len(results) - len(kept)


def parse_cited_indices(answer: str, count: int) -> list[int]:
    """Collect valid material numbers cited in the answer, preserving first-seen order."""
    seen: list[int] = []
    for raw in _CITE_RE.findall(answer or ""):
        try:
            index = int(raw)
        except ValueError:
            continue
        if 1 <= index <= count and index not in seen:
            seen.append(index)
    return seen


def filter_citations_by_answer(
    results: list[dict[str, Any]], answer: str
) -> tuple[list[dict[str, Any]], list[int]]:
    """Keep only the materials the answer cites, in first-seen citation order."""
    cited = parse_cited_indices(answer, len(results))
    return [results[index - 1] for index in cited], cited


def build_messages(
    query: str,
    items: list[dict[str, Any]],
    bodies: dict[int, str],
    per_limit: int,
    omitted: int = 0,
    labels: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Build chat messages with boundary-marked materials and non-citable pattern hints."""
    units = "\n".join(
        material_block(index + 1, item, bodies[int(item["id"])])
        for index, item in enumerate(items)
    )
    if omitted > 0:
        marker = omitted_tag(omitted)
        units = f"{units}\n{marker}" if units else marker
    content = f'{query}\n\nMaterials:\n<materials count="{len(items)}">\n{units}\n</materials>'
    pattern_block = build_pattern_block(labels or [], per_limit)
    if pattern_block:
        content = f"{content}\n\nPattern hints:\n{pattern_block}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def build_citations(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract citations with replay locators preserving retrieval order."""
    cites = []
    for item in results:
        cites.append(
            {
                "url": item["url"],
                "title": item.get("title"),
                "id": int(item["id"]),
                "library": item.get("library"),
                "version": item.get("version"),
                "source_group_id": str(item.get("source_group_id", "") or ""),
                "chunk_id": str(item.get("chunk_id", "") or ""),
                "page_id": item.get("page_id"),
                "sort_order": item.get("sort_order"),
            }
        )
    return cites
