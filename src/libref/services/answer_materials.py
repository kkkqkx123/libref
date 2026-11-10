"""Answer materials: English prompts and pure message builders for grounded QA."""

from __future__ import annotations

from typing import Any

from libref.text.snippet import truncate_material

SYSTEM_PROMPT = (
    "Answer only using the provided materials. "
    "If the materials are insufficient, say you do not know. "
    "Cite sources using material numbers like [1], [2]."
)


def material_block(
    index: int, item: dict[str, Any], full: str | None, limit: int
) -> str:
    """Render one numbered material with title, url, and truncated body."""
    title = item.get("title") or item.get("url")
    body = truncate_material(full, limit) if full else (item.get("snippet") or "")
    return f"[{index}] {title}\n{item.get('url')}\n{body}"


def build_messages(
    query: str, results: list[dict[str, Any]], full_by_id: dict[int, str], limit: int
) -> list[dict[str, str]]:
    """Build chat messages with numbered materials appended to the query."""
    materials = "\n\n".join(
        material_block(i + 1, item, full_by_id.get(int(item["id"])), limit)
        for i, item in enumerate(results)
    )
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
