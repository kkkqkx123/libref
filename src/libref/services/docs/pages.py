"""Page reads: reassemble stored document blocks into boundary-marked pages."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from libref.text.snippet import unit_tag, xml_escape_attr


def _chunk_unit(
    content: str | None,
    meta: dict[str, Any],
    sort_order: int,
) -> str:
    body = content or ""
    attrs = {
        "chunk": str(meta.get("chunk_id", "") or ""),
        "group": str(meta.get("source_group_id", "") or ""),
        "order": str(sort_order),
        "index": str(int(meta.get("chunk_index", 0) or 0)),
        "total": str(max(1, int(meta.get("total_chunks", 1) or 1))),
    }
    return unit_tag(attrs, body)


def _chunk_locator(content: str | None, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_group_id": str(meta.get("source_group_id", "") or ""),
        "chunk_id": str(meta.get("chunk_id", "") or ""),
        "span_kind": str(meta.get("span_kind", "document_range") or "document_range"),
        "merged_from": list(meta.get("merged_from", []) or []),
        "overlap_from": meta.get("overlap_from"),
        "overlap_text": str(meta.get("overlap_text", "") or ""),
        "chunk_index": int(meta.get("chunk_index", 0) or 0),
        "total_chunks": max(1, int(meta.get("total_chunks", 1) or 1)),
        "length": len(content or ""),
    }


def _doc_envelope(
    url: str,
    title: object,
    path: object,
    units: list[str],
    extra_attrs: Mapping[str, object] | None = None,
) -> str:
    rendered = "".join(
        f' {key}="{xml_escape_attr(value)}"'
        for key, value in (extra_attrs or {}).items()
    )
    head = (
        f'<doc url="{xml_escape_attr(url)}" '
        f'title="{xml_escape_attr(title)}" '
        f'path="{xml_escape_attr(path)}"{rendered}>'
    )
    return head + "\n" + "\n".join(units) + "\n</doc>"


def _parse_meta(raw: object) -> dict[str, Any]:
    try:
        return json.loads(raw) if raw else {}  # type: ignore[arg-type]
    except ValueError:
        return {}


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
        limit: int | None = None,
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
        tail = "" if limit is None else " LIMIT ?"
        if limit is not None:
            params.append(max(1, int(limit)))
        rows = self._conn.execute(
            "SELECT p.id, d.content, d.metadata, d.sort_order, l.name, v.version "
            "FROM pages p "
            "JOIN versions v ON v.id = p.version_id "
            "JOIN libraries l ON l.id = v.library_id "
            "LEFT JOIN documents d ON d.page_id = p.id "
            "AND (json_extract(d.metadata, '$.chunk_path') IS NULL "
            "OR json_extract(d.metadata, '$.chunk_path') != 'bm25') "
            f"WHERE {' AND '.join(filters)} ORDER BY d.sort_order{tail}",
            params,
        ).fetchall()
        if not rows:
            return None
        meta: dict[str, Any] = {}
        for r in rows:
            parsed_head = _parse_meta(r[2])
            if parsed_head:
                meta = parsed_head
                break
        units: list[str] = []
        details: list[dict[str, Any]] = []
        for content, raw, order in ((r[1], r[2], r[3]) for r in rows):
            if content is None:
                continue
            parsed = _parse_meta(raw)
            units.append(_chunk_unit(content, parsed, int(order or 0)))
            details.append(_chunk_locator(content, parsed))
        content = _doc_envelope(url, meta.get("title"), meta.get("path"), units)
        return {
            "url": url,
            "library": rows[0][4],
            "version": rows[0][5],
            "title": meta.get("title"),
            "path": meta.get("path"),
            "content": content,
            "chunks": len(units),
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
            "SELECT p.id, l.name, v.version FROM pages p"
            " JOIN versions v ON v.id = p.version_id "
            "JOIN libraries l ON l.id = v.library_id "
            f"WHERE {' AND '.join(filters)}",
            params,
        ).fetchone()
        if row is None:
            return None
        from libref.store.document_store import DocumentStore

        store = DocumentStore(self._conn)
        rows = store.get_window(int(row[0]), sort_order, radius, chunk_path="embedding")
        head_meta: dict[str, Any] = {}
        for entry in rows:
            candidate = entry.get("metadata") or {}
            if candidate.get("title") or candidate.get("path"):
                head_meta = candidate
                break
        units: list[str] = []
        docs: list[dict[str, Any]] = []
        for entry in rows:
            raw_meta = entry.get("metadata") or {}
            units.append(
                _chunk_unit(
                    entry.get("content"), raw_meta, int(entry.get("sort_order") or 0)
                )
            )
            locator = _chunk_locator(entry.get("content"), raw_meta)
            locator.update(
                {
                    "id": int(entry.get("id") or 0),
                    "url": str(entry.get("url") or ""),
                    "sort_order": int(entry.get("sort_order") or 0),
                }
            )
            docs.append(locator)
        content = _doc_envelope(
            url,
            head_meta.get("title"),
            head_meta.get("path"),
            units,
            {"mode": "window"},
        )
        return {
            "url": url,
            "center": sort_order,
            "radius": radius,
            "content": content,
            "docs": docs,
        }

    def read_section(
        self,
        url: str,
        source_group_id: str,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
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
        filters.append(
            "(json_extract(d.metadata, '$.chunk_path') IS NULL"
            " OR json_extract(d.metadata, '$.chunk_path') != 'bm25')"
        )
        tail = "" if limit is None else " LIMIT ?"
        if limit is not None:
            params.append(max(1, int(limit)))
        rows = self._conn.execute(
            "SELECT d.content, d.metadata, d.sort_order FROM documents d "
            "JOIN versions v ON v.id = d.version_id "
            "JOIN libraries l ON l.id = d.library_id "
            f"WHERE {' AND '.join(filters)} ORDER BY d.sort_order{tail}",
            params,
        ).fetchall()
        if not rows:
            return None
        units: list[str] = []
        details: list[dict[str, Any]] = []
        for content, raw, order in rows:
            parsed = _parse_meta(raw)
            units.append(_chunk_unit(content, parsed, int(order or 0)))
            details.append(_chunk_locator(content, parsed))
        content = _doc_envelope(
            url,
            None,
            None,
            units,
            {"group": source_group_id},
        )
        return {
            "url": url,
            "source_group_id": source_group_id,
            "content": content,
            "chunks": len(units),
            "chunk_details": details,
        }

    def read_doc(
        self,
        url: str,
        mode: str = "page",
        sort_order: int | None = None,
        radius: int = 1,
        source_group_id: str | None = None,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
    ) -> dict[str, Any] | None:
        """Unified read: full page, neighboring window, or whole section by mode."""
        if mode == "page":
            return self.read_page(
                url, library=library, version=version, ecosystem=ecosystem
            )
        if mode == "window":
            if sort_order is None:
                raise ValueError("window mode requires sort_order")
            return self.read_window(
                url,
                sort_order,
                radius,
                library=library,
                version=version,
                ecosystem=ecosystem,
            )
        if mode == "section":
            if not source_group_id:
                raise ValueError("section mode requires source_group_id")
            return self.read_section(
                url,
                source_group_id,
                library=library,
                version=version,
                ecosystem=ecosystem,
            )
        raise ValueError(f"unsupported read mode: {mode}")
