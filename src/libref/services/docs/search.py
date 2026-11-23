"""Search orchestration: the single preset router over storage primitives."""

from __future__ import annotations

import json
from typing import Any

from libref.text.embedding import strip_markdown_for_embedding
from libref.text.snippet import make_snippet

PRESETS = ("precise", "balanced", "thorough")


def normalize_preset(preset: str) -> str:
    if preset not in PRESETS:
        raise ValueError(f"unsupported search preset: {preset}")
    return preset


class SearchService:
    """Retrieval orchestration and preset routing; page and mining reads live elsewhere."""

    def __init__(
        self, conn, settings=None, *, vec_store=None, embed=None, rerank=None
    ) -> None:
        from libref.store.vec_store import paired_vector

        self._conn = conn
        self._settings = settings
        self._vec, self._embed = paired_vector(vec_store, embed)
        self._rerank = rerank

    def search_docs(
        self,
        query: str,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 8,
        tenant_id: str | None = None,
        block_type: str | None = None,
        exclude_deprecated: bool = False,
        aligned_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Exact full-text query: the FTS single path used by the precise preset."""
        from libref.config import setting_or_default
        from libref.store.retriever import (
            alignment_key,
            filter_by_doc_facets,
            fts_search,
        )

        weights = (
            float(setting_or_default(self._settings, "fts_w_content")),
            float(setting_or_default(self._settings, "fts_w_title")),
            float(setting_or_default(self._settings, "fts_w_url")),
            float(setting_or_default(self._settings, "fts_w_path")),
        )
        radius = int(setting_or_default(self._settings, "snippet_radius"))
        cap = int(setting_or_default(self._settings, "snippet_cap"))
        if tenant_id not in (None, "*") and library is not None and version is not None:
            from libref.services.infra.tenant import (
                is_global,
                resolve_version_id,
                table_has_tenant,
            )

            scoped = not is_global(str(tenant_id)) and (
                table_has_tenant(self._conn, "libraries")
                or table_has_tenant(self._conn, "versions")
            )
            if scoped:
                try:
                    resolve_version_id(
                        self._conn, str(tenant_id), library, version, ecosystem
                    )
                except ValueError:
                    return []
        # Extra picks compensate rows folded away by group dedup below.
        budget = max(limit, min(limit * 5, 500))
        pairs = fts_search(
            self._conn,
            query,
            library=library,
            version=version,
            ecosystem=ecosystem,
            weights=weights,
            budget=budget,
            tenant_id=tenant_id,
        )
        if not pairs:
            return []
        marks = ",".join("?" for _ in pairs)
        from libref.services.infra.tenant import is_global as _is_g
        from libref.services.infra.tenant import table_has_tenant as _has_t

        scoped_doc = (
            tenant_id not in (None, "*")
            and not _is_g(str(tenant_id or ""))
            and _has_t(self._conn, "documents")
        )
        extra = " AND d.tenant_id = ?" if scoped_doc else ""
        extra_params: list[Any] = [str(tenant_id)] if scoped_doc else []
        rows = self._conn.execute(
            "SELECT d.id, d.url, d.content, "
            "json_extract(d.metadata, '$.title') AS title, "
            "json_extract(d.metadata, '$.path') AS path, "
            "l.name AS library, v.version AS version, d.metadata, "
            "d.page_id, d.sort_order "
            "FROM documents d "
            "JOIN versions v ON v.id = d.version_id "
            "JOIN libraries l ON l.id = d.library_id "
            f"WHERE d.id IN ({marks}){extra}",
            [doc_id for doc_id, _ in pairs] + extra_params,
        ).fetchall()
        by_id = {int(r[0]): r for r in rows}
        scores = {doc_id: -score for doc_id, score in pairs}
        ordered = [by_id[i] for i, _ in pairs if i in by_id]
        out = []
        seen: set[str] = set()
        for r in ordered:
            try:
                meta = json.loads(r[7]) if r[7] else {}
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
                    "score": scores[int(r[0])],
                    "snippet": make_snippet(
                        strip_markdown_for_embedding(r[2]),
                        query,
                        radius=radius,
                        cap=cap,
                    ),
                    "title": r[3],
                    "path": r[4],
                    "library": r[5],
                    "version": r[6],
                    "source_group_id": meta.get("source_group_id", ""),
                    "chunk_id": meta.get("chunk_id", ""),
                    "page_id": r[8],
                    "sort_order": r[9],
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
        kept = filter_by_doc_facets(
            self._conn,
            [int(item["id"]) for item in out],
            block_type=block_type,
            exclude_deprecated=exclude_deprecated,
            aligned_only=aligned_only,
        )
        keep_set = set(kept)
        return [item for item in out if int(item["id"]) in keep_set][:limit]

    async def search_with_mode(
        self,
        query: str,
        preset: str = "balanced",
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 8,
        expand_window: bool | None = None,
        tenant_id: str | None = None,
        block_type: str | None = None,
        exclude_deprecated: bool = False,
        aligned_only: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Route presets: precise keeps the exact full-text path, the others fuse recall.

        Window expansion follows the caller's explicit preference, then the preset, then
        the configuration; an unspecified preference defers to the latter two.
        """
        from libref.store.evidence import pool_texts, script_mismatch, verdict
        from libref.store.retriever import HybridRetriever

        use_preset = normalize_preset(preset)
        if use_preset == "precise":
            results = self.search_docs(
                query,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit,
                tenant_id=tenant_id,
                block_type=block_type,
                exclude_deprecated=exclude_deprecated,
                aligned_only=aligned_only,
            )
            pool = pool_texts(self._conn, [int(r["id"]) for r in results])
            # The lexical lane cannot answer a query whose script the corpus never
            # uses; the flag marks the downgrade so the benchmark does not score it
            # as a missed retrieval.
            cross_lingual = script_mismatch(query, list(pool.values()))
            return results, {
                "degraded": False,
                "reasons": [],
                "fts_count": len(results),
                "vec_count": 0,
                "fused_order": [int(r["id"]) for r in results],
                "rerank_applied": False,
                "cross_lingual": cross_lingual,
                **verdict(
                    query,
                    results,
                    pool,
                    self._settings,
                ),
            }
        if expand_window is not None:
            expand: bool | None = bool(expand_window)
        elif use_preset == "thorough":
            expand = True
        else:
            expand = None
        retriever = HybridRetriever(
            self._conn,
            self._settings,
            vec_store=self._vec,
            embed=self._embed,
            rerank=self._rerank,
        )
        return await retriever.search_async(
            query,
            library=library,
            version=version,
            ecosystem=ecosystem,
            limit=limit,
            expand_window=expand,
            tenant_id=tenant_id,
            block_type=block_type,
            exclude_deprecated=exclude_deprecated,
            aligned_only=aligned_only,
        )
