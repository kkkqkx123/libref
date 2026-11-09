"""Search orchestration: FTS-only and hybrid retrieval over storage primitives."""

from __future__ import annotations

import json
from typing import Any

from libref.text.embedding import strip_markdown_for_embedding
from libref.text.query import safe_fts_query
from libref.text.snippet import make_snippet


class SearchService:
    """Thin retrieval orchestration; page and mining reads live elsewhere."""

    def __init__(self, conn, settings=None, *, vec_store=None, embed=None, rerank=None) -> None:
        self._conn = conn
        self._settings = settings
        self._vec = vec_store if embed is not None else None
        self._embed = embed if vec_store is not None else None
        self._rerank = rerank

    def search_docs(
        self,
        query: str,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """精确全文查：只走 FTS 单路，混合默认查走异步混合入口。"""
        from libref.config import setting_or_default

        filters = []
        safe = safe_fts_query(query)
        if not safe:
            return []
        weights = (
            float(setting_or_default(self._settings, "fts_w_content")),
            float(setting_or_default(self._settings, "fts_w_title")),
            float(setting_or_default(self._settings, "fts_w_url")),
            float(setting_or_default(self._settings, "fts_w_path")),
        )
        radius = int(setting_or_default(self._settings, "snippet_radius"))
        cap = int(setting_or_default(self._settings, "snippet_cap"))
        params: list[Any] = [safe]
        if library is not None:
            filters.append("l.name = ?")
            params.append(library)
        if ecosystem is not None:
            filters.append("l.ecosystem = ?")
            params.append(ecosystem)
        if version is not None:
            filters.append("v.version = ?")
            params.append(version)
        where = ("AND " + " AND ".join(filters) + " ") if filters else ""
        params.append(limit)
        sql = (
            f"SELECT d.id, d.url, bm25(documents_fts, {weights[0]}, {weights[1]}, {weights[2]}, {weights[3]}) AS score, d.content, "
            "json_extract(d.metadata, '$.title') AS title, "
            "json_extract(d.metadata, '$.path') AS path, "
            "l.name AS library, v.version AS version, d.metadata, "
            "d.page_id, d.sort_order "
            "FROM documents_fts "
            "JOIN documents d ON d.id = documents_fts.rowid "
            "JOIN versions v ON v.id = d.version_id "
            "JOIN libraries l ON l.id = d.library_id "
            f"WHERE documents_fts MATCH ? {where}"
            "ORDER BY score LIMIT ?"
        )
        rows = self._conn.execute(sql, params).fetchall()
        out = []
        from libref.store.retriever import alignment_key
        seen: set[str] = set()
        for r in rows:
            try:
                meta = json.loads(r[8]) if r[8] else {}
            except ValueError:
                meta = {}
            key = alignment_key(meta, r[1], int(r[0]))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "id": r[0],
                    "url": r[1],
                    "score": r[2],
                    "snippet": make_snippet(strip_markdown_for_embedding(r[3]), query, radius=radius, cap=cap),
                    "title": r[4],
                    "path": r[5],
                    "library": r[6],
                    "version": r[7],
                    "source_group_id": meta.get("source_group_id", ""),
                    "chunk_id": meta.get("chunk_id", ""),
                    "page_id": r[9],
                    "sort_order": r[10],
                    "span_kind": meta.get("span_kind", "document_range"),
                    "merged_from": list(meta.get("merged_from", []) or []),
                    "overlap_from": meta.get("overlap_from"),
                    "overlap_text": str(meta.get("overlap_text", "") or ""),
                    "chunk_index": int(meta.get("chunk_index", 0) or 0),
                    "total_chunks": max(1, int(meta.get("total_chunks", 1) or 1)),
                    "keywords": list(meta.get("keywords", []) or []),
                    "content_type": meta.get("content_type", "text/markdown"),
                    "entities": list(meta.get("entities", []) or []),
                }
            )
        return out

    async def search_hybrid_async(
        self,
        query: str,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 8,
        vec_store=None,
        embed=None,
        rerank=None,
        expand_window: bool | None = None,
        window_radius: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """混合默认查：双路召回融合，失败回落全文并标记降级。"""
        from libref.store.retriever import HybridRetriever

        retriever = HybridRetriever(
            self._conn,
            self._settings,
            vec_store=vec_store if vec_store is not None else self._vec,
            embed=embed if embed is not None else self._embed,
            rerank=rerank if rerank is not None else self._rerank,
        )
        return await retriever.search_async(
            query, library=library, version=version,
            ecosystem=ecosystem, limit=limit,
            expand_window=expand_window, window_radius=window_radius,
        )
