"""Hybrid retrieval orchestration."""

from __future__ import annotations

import asyncio
from typing import Any

from libref.store.retriever.assembly import Assembly
from libref.store.retriever.candidates import (
    _current_vids,
    _fts_candidates,
    _vec_search_scoped,
    _version_ids,
)
from libref.store.retriever.features import filter_by_doc_facets
from libref.store.retriever.fusion import (
    Fusion,
    _collapse_duplicates,
    _cross_lingual_miss,
)

__all__ = ["HybridRetriever", "build_retriever"]


class HybridRetriever(Assembly, Fusion):
    def __init__(
        self, conn, settings=None, *, vec_store=None, embed=None, rerank=None
    ) -> None:
        from libref.store.vec_store import paired_vector

        self._conn = conn
        self._settings = settings
        self._vec, self._embed = paired_vector(vec_store, embed)
        self._rerank = rerank

    async def search_async(
        self,
        query: str,
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
        """Hybrid retrieval, returning results plus a status map with degraded reasons and road counts.

        Window expansion follows the configuration unless the caller states a preference.
        """
        from libref.config import setting_or_default
        from libref.store.evidence import pool_texts, verdict

        if not query.strip():
            raise ValueError("query is empty")
        if limit <= 0:
            raise ValueError("limit must be positive")
        degraded = False
        reasons: list[str] = []
        candidate_floor = int(
            setting_or_default(self._settings, "rerank_candidate_limit")
        )
        candidate_mult = int(setting_or_default(self._settings, "candidate_multiplier"))
        recall_budget = max(candidate_mult * limit, candidate_floor)
        if block_type or exclude_deprecated or aligned_only:
            recall_budget *= candidate_mult
        assemble_mult = int(setting_or_default(self._settings, "assemble_multiplier"))
        assemble_min = int(setting_or_default(self._settings, "assemble_min"))
        assemble_window = max(assemble_mult * limit, assemble_min)
        need = recall_budget
        weights = (
            float(setting_or_default(self._settings, "fts_w_content")),
            float(setting_or_default(self._settings, "fts_w_title")),
            float(setting_or_default(self._settings, "fts_w_url")),
            float(setting_or_default(self._settings, "fts_w_path")),
        )
        fts_ids = _fts_candidates(
            self._conn,
            query,
            library,
            version,
            ecosystem,
            need,
            weights=weights,
            tenant_id=tenant_id,
        )
        vec_ids: list[int] = []
        vids = _version_ids(self._conn, library, version, ecosystem, tenant_id)
        max_versions = int(setting_or_default(self._settings, "retrieval_max_versions"))
        if self._vec is not None and self._embed is not None and bool(vids):
            vids, drift_skipped = _current_vids(self._conn, self._settings, vids)
            if drift_skipped:
                degraded = True
                reasons.append("version_drift")
            if vids:
                try:
                    [query_vec] = await self._embed([query])
                except Exception:  # noqa: BLE001 -- vector failure marks degraded, fall back to FTS-only
                    degraded = True
                    reasons.append("vector_error")
                    query_vec = []
                if query_vec is not None and len(query_vec):
                    if len(vids) == 1:
                        try:
                            vec_ids = [
                                doc_id
                                for doc_id, _ in _vec_search_scoped(
                                    self._vec,
                                    query_vec,
                                    k=need,
                                    version_id=vids[0],
                                    tenant_id=tenant_id,
                                )
                            ]
                        except Exception:  # noqa: BLE001 -- vector failure marks degraded, fall back to FTS-only
                            degraded = True
                            reasons.append("vector_error")
                            vec_ids = []
                    else:
                        scoped = vids
                        if len(scoped) > max_versions:
                            scoped = scoped[:max_versions]
                            degraded = True
                            reasons.append("version_capped")
                        try:
                            grouped = await asyncio.gather(
                                *(
                                    asyncio.to_thread(
                                        _vec_search_scoped,
                                        self._vec,
                                        query_vec,
                                        k=need,
                                        version_id=vid,
                                        tenant_id=tenant_id,
                                    )
                                    for vid in scoped
                                )
                            )
                        except Exception:  # noqa: BLE001 -- vector failure marks degraded, fall back to FTS-only
                            degraded = True
                            reasons.append("vector_error")
                            vec_ids = []
                        else:
                            pooled: list[tuple[float, int]] = [
                                (float(score), int(doc_id))
                                for hits in grouped
                                for doc_id, score in hits
                            ]
                            pooled.sort(key=lambda kv: (kv[0], kv[1]), reverse=True)
                            seen_ids: set[int] = set()
                            merged: list[int] = []
                            for _, doc_id in pooled:
                                if doc_id in seen_ids:
                                    continue
                                seen_ids.add(doc_id)
                                merged.append(doc_id)
                                if len(merged) >= need:
                                    break
                            vec_ids = merged
        fused = self._rrf(fts_ids, vec_ids)
        fused_order = list(fused)
        fused = _collapse_duplicates(self._conn, fused)
        fused = filter_by_doc_facets(
            self._conn,
            fused,
            block_type=block_type,
            exclude_deprecated=exclude_deprecated,
            aligned_only=aligned_only,
        )
        cross_lingual = _cross_lingual_miss(query, fts_ids, vec_ids)
        if not fused:
            return [], self._status(
                degraded,
                reasons,
                fts_ids,
                vec_ids,
                fused_order,
                False,
                verdict(query, [], {}),
                cross_lingual,
            )
        fused, rerank_scores, rerank_failed = await self._apply_rerank(
            query,
            fused,
            window=assemble_window,
            top_n=int(setting_or_default(self._settings, "rerank_top_n")),
        )
        rerank_applied = bool(rerank_scores)
        if rerank_failed:
            degraded = True
            reasons.append("rerank_error")
        expand = (
            bool(setting_or_default(self._settings, "retriever_expand_window"))
            if expand_window is None
            else bool(expand_window)
        )
        radius = int(setting_or_default(self._settings, "retriever_window_radius"))
        results, window_ok = self._postprocess(
            query,
            fused[:assemble_window],
            fts_ids,
            vec_ids,
            expand_window=expand,
            window_radius=int(radius),
            rerank_scores=rerank_scores,
        )
        if not window_ok:
            degraded = True
            reasons.append("window_error")
        return results[:limit], self._status(
            degraded,
            reasons,
            fts_ids,
            vec_ids,
            fused_order,
            rerank_applied,
            verdict(
                query,
                results[:limit],
                pool_texts(self._conn, fused[:assemble_window]),
                self._settings,
            ),
            cross_lingual,
        )


def build_retriever(settings, conn, *, vec_store=None, embed=None, rerank=None):
    """Build a hybrid retriever from settings; degrades to FTS-only when vectors are missing."""
    return HybridRetriever(
        conn, settings, vec_store=vec_store, embed=embed, rerank=rerank
    )
