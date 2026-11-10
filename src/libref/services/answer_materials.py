"""Answer materials: grounded QA prompts and boundary-marked message builders."""

from __future__ import annotations

from typing import Any

from libref.text.snippet import omitted_tag, truncate_material, unit_tag

SYSTEM_PROMPT = (
    "Answer only using the provided materials. "
    "If the materials are insufficient, say you do not know. "
    "Cite sources using material numbers like [1], [2]."
)


def material_block(
    index: int, item: dict[str, Any], full: str | None, limit: int
) -> str:
    """Render one numbered material as a self-locating unit with verbatim body."""
    body = truncate_material(full, limit) if full else (item.get("snippet") or "")
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


def build_label_blocks(labels: list[dict[str, Any]], limit: int) -> str:
    """Render pattern labels as extra grounded materials."""
    if not labels:
        return ""
    units: list[str] = []
    for index, label in enumerate(labels[: max(1, int(limit))]):
        pattern = label.get("pattern") or []
        body = str(label.get("summary") or "")
        workflow = str(label.get("workflow") or "")
        if workflow:
            body = f"{body} Workflow: {workflow}" if body else workflow
        attrs = {
            "n": f"label-{index + 1}",
            "name": str(label.get("name") or ""),
            "pattern": ", ".join(str(p) for p in pattern),
        }
        units.append(unit_tag(attrs, truncate_material(body, limit)))
    return "\n".join(units)


def build_messages(
    query: str,
    results: list[dict[str, Any]],
    full_by_id: dict[int, str],
    limit: int,
    labels: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Build chat messages with boundary-marked materials appended to the query."""
    units = "\n".join(
        material_block(i + 1, item, full_by_id.get(int(item["id"])), limit)
        for i, item in enumerate(results)
    )
    if len(results) > 0 and not units.strip():
        units = omitted_tag(len(results))
    label_block = build_label_blocks(labels or [], limit)
    if label_block:
        units = f"{units}\n{label_block}" if units.strip() else label_block
    materials = f'<materials count="{len(results)}">\n{units}\n</materials>'
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"{query}\n\nMaterials:\n{materials}"},
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
