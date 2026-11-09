"""Page reads: reassemble stored document blocks into full pages and windows."""

from __future__ import annotations

import json
from typing import Any


class PageService:
    """Read-only page and section access over pages and documents tables."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def read_page(
        self,
        url: str,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch all document blocks for a URL ordered by sort_order."""
        filters = ["p.url = ?"]
        params: list[Any] = [url]
        if library is not None:
            filters.append("l.name = ?")
            params.append(library)
        if ecosystem is not None:
            filters.append("l.ecosystem = ?")
            params.append(ecosystem)
        if version is not None:
            filters.append("v.version = ?")
            params.append(version)
        rows = self._conn.execute(
            "SELECT p.id, d.content, d.metadata, d.sort_order, l.name, v.version "
            "FROM pages p "
            "JOIN versions v ON v.id = p.version_id "
            "JOIN libraries l ON l.id = v.library_id "
            "LEFT JOIN documents d ON d.page_id = p.id "
            "AND (json_extract(d.metadata, '$.chunk_path') IS NULL "
            "OR json_extract(d.metadata, '$.chunk_path') != 'bm25') "
            f"WHERE {' AND '.join(filters)} ORDER BY d.sort_order",
            params,
        ).fetchall()
        if not rows:
            return None
        contents = [r[1] for r in rows if r[1] is not None]
        meta = {}
        if rows[0][2]:
            meta = json.loads(rows[0][2])
        details = []
        for content, raw, _order in ((r[1], r[2], None) for r in rows):
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {}
            details.append({
                "content": content,
                "source_group_id": parsed.get("source_group_id", ""),
                "chunk_id": parsed.get("chunk_id", ""),
                "span_kind": parsed.get("span_kind", "document_range"),
                "merged_from": list(parsed.get("merged_from", []) or []),
                "overlap_from": parsed.get("overlap_from"),
            })
        return {
            "url": url,
            "library": rows[0][4],
            "version": rows[0][5],
            "title": meta.get("title"),
            "path": meta.get("path"),
            "content": "\n\n".join(contents),
            "chunks": len(contents),
            "chunk_details": details,
        }

    def read_window(
        self,
        url: str,
        sort_order: int,
        radius: int = 1,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch neighboring blocks around a sort position for hit expansion."""
        page = self.read_page(url, library=library, version=version, ecosystem=ecosystem)
        if page is None:
            return None
        filters = ["p.url = ?"]
        params: list[Any] = [url]
        if library is not None:
            filters.append("l.name = ?")
            params.append(library)
        if ecosystem is not None:
            filters.append("l.ecosystem = ?")
            params.append(ecosystem)
        if version is not None:
            filters.append("v.version = ?")
            params.append(version)
        row = self._conn.execute(
            "SELECT p.id FROM pages p JOIN versions v ON v.id = p.version_id "
            "JOIN libraries l ON l.id = v.library_id "
            f"WHERE {' AND '.join(filters)}",
            params,
        ).fetchone()
        if row is None:
            return None
        from libref.store.document_store import DocumentStore
        docs = DocumentStore(self._conn).get_window(int(row[0]), sort_order, radius)
        return {"url": url, "center": sort_order, "radius": radius, "docs": docs}

    def read_section(
        self,
        url: str,
        source_group_id: str,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch a whole source group to trace a sub-block back to its section."""
        filters = ["d.url = ?", "json_extract(d.metadata, '$.source_group_id') = ?"]
        params: list[Any] = [url, source_group_id]
        if library is not None:
            filters.append("l.name = ?")
            params.append(library)
        if ecosystem is not None:
            filters.append("l.ecosystem = ?")
            params.append(ecosystem)
        if version is not None:
            filters.append("v.version = ?")
            params.append(version)
        rows = self._conn.execute(
            "SELECT d.content, d.metadata, d.sort_order FROM documents d "
            "JOIN versions v ON v.id = d.version_id "
            "JOIN libraries l ON l.id = d.library_id "
            f"WHERE {' AND '.join(filters)} ORDER BY d.sort_order",
            params,
        ).fetchall()
        if not rows:
            return None
        details = []
        for content, raw, _order in rows:
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {}
            details.append({
                "content": content,
                "source_group_id": parsed.get("source_group_id", ""),
                "chunk_id": parsed.get("chunk_id", ""),
                "span_kind": parsed.get("span_kind", "document_range"),
                "merged_from": list(parsed.get("merged_from", []) or []),
                "overlap_from": parsed.get("overlap_from"),
            })
        return {
            "url": url,
            "source_group_id": source_group_id,
            "content": "\n\n".join(r[0] for r in rows if r[0]),
            "chunks": len(rows),
            "chunk_details": details,
        }
