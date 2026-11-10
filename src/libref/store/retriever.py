"""Storage-level retrieval primitives and hybrid fusion over SQLite and Qdrant.

SQL access stays here. String shaping lives in libref.text, mining
weighting lives in libref.mining.scoring.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

from libref.mining.scoring import (
    cluster_hit,
    cluster_hit_exact,
    support_for_content,
    support_for_entities,
)
from libref.text.embedding import strip_markdown_for_embedding
from libref.text.query import safe_fts_query
from libref.text.snippet import make_snippet, truncate_material


def _version_ids(
    conn: sqlite3.Connection,
    library: str | None,
    version: str | None,
    ecosystem: str | None,
) -> list[int]:
    filters = []
    params: list[Any] = []
    if library is not None:
        filters.append("l.name = ?")
        params.append(library)
    if ecosystem is not None:
        filters.append("l.ecosystem = ?")
        params.append(ecosystem)
    if version is not None:
        filters.append("v.version = ?")
        params.append(version)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    rows = conn.execute(
        f"SELECT v.id FROM versions v JOIN libraries l ON l.id = v.library_id {where}",
        params,
    ).fetchall()
    return [int(r[0]) for r in rows]


def _fts_candidates(
    conn: sqlite3.Connection,
    query: str,
    library: str | None,
    version: str | None,
    ecosystem: str | None,
    limit: int,
    *,
    weights: tuple[float, float, float, float],
) -> list[int]:
    safe = safe_fts_query(query)
    if not safe:
        return []
    filters = []
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
    rows = conn.execute(
        "SELECT d.id FROM documents_fts "
        "JOIN documents d ON d.id = documents_fts.rowid "
        "JOIN versions v ON v.id = d.version_id "
        "JOIN libraries l ON l.id = d.library_id "
        f"WHERE documents_fts MATCH ? {where}"
        f"ORDER BY bm25(documents_fts, {weights[0]}, {weights[1]}, {weights[2]}, {weights[3]}) LIMIT ?",
        params,
    ).fetchall()
    return [int(r[0]) for r in rows]


def _fetch_details(
    conn: sqlite3.Connection,
    doc_ids: list[int],
    query: str,
    *,
    snippet_radius: int,
    snippet_cap: int,
) -> dict[int, dict[str, Any]]:
    if not doc_ids:
        return {}
    marks = ",".join("?" for _ in doc_ids)
    rows = conn.execute(
        "SELECT d.id, d.url, d.content, json_extract(d.metadata, '$.title'), "
        "json_extract(d.metadata, '$.path'), l.name, v.version, d.version_id, "
        "d.metadata, d.page_id, d.sort_order "
        "FROM documents d "
        "JOIN versions v ON v.id = d.version_id "
        "JOIN libraries l ON l.id = d.library_id "
        f"WHERE d.id IN ({marks})",
        doc_ids,
    ).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        try:
            meta = json.loads(r[8]) if r[8] else {}
        except ValueError:
            meta = {}
        out[int(r[0])] = {
            "id": int(r[0]),
            "url": r[1],
            "snippet": make_snippet(
                strip_markdown_for_embedding(r[2]),
                query,
                radius=snippet_radius,
                cap=snippet_cap,
            ),
            "title": r[3],
            "path": r[4],
            "library": r[5],
            "version": r[6],
            "version_id": int(r[7]),
            "content": r[2] or "",
            "chunk_path": meta.get("chunk_path", "embedding"),
            "source_group_id": meta.get("source_group_id", ""),
            "chunk_id": meta.get("chunk_id", ""),
            "span_kind": meta.get("span_kind", "document_range"),
            "merged_from": list(meta.get("merged_from", []) or []),
            "overlap_from": meta.get("overlap_from"),
            "overlap_text": str(meta.get("overlap_text", "") or ""),
            "chunk_index": int(meta.get("chunk_index", 0) or 0),
            "total_chunks": max(1, int(meta.get("total_chunks", 1) or 1)),
            "keywords": list(meta.get("keywords", []) or []),
            "content_type": meta.get("content_type", "text/markdown"),
            "entities": list(meta.get("entities", []) or []),
            "page_id": int(r[9]),
            "sort_order": int(r[10]),
        }
    return out


def alignment_key(meta: dict[str, Any] | None, url: str | None, doc_id: int) -> str:
    """Cross-path alignment key: entities first, document section next, chunk id as fallback."""
    data = meta or {}
    entities = data.get("entities") or []
    if entities:
        first = str(entities[0]).strip()
        if first:
            return f"e:{first}"
    group = str(data.get("source_group_id") or "").strip()
    if group:
        return f"s:{url or ''}\x00{group}"
    chunk_id = str(data.get("chunk_id") or "").strip()
    if chunk_id:
        return f"c:{chunk_id}"
    return f"c:{doc_id}"


def _collapse_by_group(conn: sqlite3.Connection, fused: list[int]) -> list[int]:
    """Keep only the highest-ranked chunk per source group, merging twin chunks from both paths into one hit."""
    if len(fused) < 2:
        return fused
    marks = ",".join("?" for _ in fused)
    rows = conn.execute(
        f"SELECT id, url, metadata FROM documents WHERE id IN ({marks})", fused
    ).fetchall()
    groups: dict[int, str] = {}
    for doc_id, url, raw in rows:
        try:
            meta = json.loads(raw) if raw else {}
        except ValueError:
            meta = {}
        groups[int(doc_id)] = alignment_key(meta, url, int(doc_id))
    seen: set[str] = set()
    out: list[int] = []
    for doc_id in fused:
        key = groups.get(doc_id, f"c:{doc_id}")
        if key in seen:
            continue
        seen.add(key)
        out.append(doc_id)
    return out


def _pattern_supports(
    conn: sqlite3.Connection, version_ids: list[int]
) -> dict[int, list[tuple[list[str], int]]]:
    if not version_ids:
        return {}
    marks = ",".join("?" for _ in version_ids)
    rows = conn.execute(
        "SELECT version_id, pattern, support FROM seq_patterns "
        f"WHERE version_id IN ({marks})",
        version_ids,
    ).fetchall()
    out: dict[int, list[tuple[list[str], int]]] = {}
    for vid, raw, support in rows:
        try:
            names = json.loads(raw)
        except ValueError:
            continue
        out.setdefault(int(vid), []).append((names, int(support)))
    return out


def _cluster_sets(
    conn: sqlite3.Connection, version_ids: list[int]
) -> dict[int, list[set[str]]]:
    if not version_ids:
        return {}
    marks = ",".join("?" for _ in version_ids)
    rows = conn.execute(
        "SELECT version_id, cluster_id, member FROM api_clusters "
        f"WHERE version_id IN ({marks})",
        version_ids,
    ).fetchall()
    grouped: dict[tuple[int, int], set[str]] = {}
    for vid, cid, member in rows:
        grouped.setdefault((int(vid), int(cid)), set()).add(str(member))
    out: dict[int, list[set[str]]] = {}
    for (vid, _), members in grouped.items():
        out.setdefault(vid, []).append(members)
    return out


def _current_vids(conn, settings, vids: list[int]) -> tuple[list[int], bool]:
    """Split version ids into vector-searchable ones; drifted ones are skipped."""
    from libref.config import setting_or_default
    from libref.store.vec_store import read_version_meta

    model = setting_or_default(settings, "embedding_model")
    dim = int(setting_or_default(settings, "embedding_dim"))
    current: list[int] = []
    skipped = False
    for vid in vids:
        old_model, old_dim = read_version_meta(conn, int(vid))
        if old_model is None and old_dim is None:
            current.append(int(vid))
        elif (old_model, old_dim) != (model, dim):
            skipped = True
        else:
            current.append(int(vid))
    return current, skipped


class HybridRetriever:
    """Orchestration point for two-path recall and fused reranking; vector and rerank failures only set the degraded flag."""

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
        window_radius: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Hybrid retrieval, returning the result list and a degraded flag."""
        from libref.config import setting_or_default

        if not query.strip():
            raise ValueError("query is empty")
        if limit <= 0:
            raise ValueError("limit must be positive")
        degraded = False
        candidate_floor = int(
            setting_or_default(self._settings, "rerank_candidate_limit")
        )
        candidate_mult = int(setting_or_default(self._settings, "candidate_multiplier"))
        need = max(candidate_mult * limit, candidate_floor)
        weights = (
            float(setting_or_default(self._settings, "fts_w_content")),
            float(setting_or_default(self._settings, "fts_w_title")),
            float(setting_or_default(self._settings, "fts_w_url")),
            float(setting_or_default(self._settings, "fts_w_path")),
        )
        fts_ids = _fts_candidates(
            self._conn, query, library, version, ecosystem, need, weights=weights
        )
        vec_ids: list[int] = []
        vids = _version_ids(self._conn, library, version, ecosystem)
        max_versions = int(setting_or_default(self._settings, "retrieval_max_versions"))
        if self._vec is not None and self._embed is not None and vids:
            vids, drift_skipped = _current_vids(self._conn, self._settings, vids)
            degraded = degraded or drift_skipped
            if vids:
                try:
                    [query_vec] = await self._embed([query])
                except Exception:  # noqa: BLE001 -- vector failure marks degraded, fall back to FTS-only
                    degraded = True
                    query_vec = []
                if query_vec is not None and len(query_vec):
                    if len(vids) == 1:
                        try:
                            vec_ids = [
                                doc_id
                                for doc_id, _ in self._vec.search(
                                    query_vec, k=need, version_id=vids[0]
                                )
                            ]
                        except Exception:  # noqa: BLE001 -- vector failure marks degraded, fall back to FTS-only
                            degraded = True
                            vec_ids = []
                    else:
                        scoped = vids
                        if len(scoped) > max_versions:
                            scoped = scoped[:max_versions]
                            degraded = True
                        try:
                            grouped = await asyncio.gather(
                                *(
                                    asyncio.to_thread(
                                        self._vec.search,
                                        query_vec,
                                        k=need,
                                        version_id=vid,
                                    )
                                    for vid in scoped
                                )
                            )
                        except Exception:  # noqa: BLE001 -- vector failure marks degraded, fall back to FTS-only
                            degraded = True
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
        fused = _collapse_by_group(self._conn, fused)
        if not fused:
            return [], degraded
        assemble_mult = int(setting_or_default(self._settings, "assemble_multiplier"))
        assemble_min = int(setting_or_default(self._settings, "assemble_min"))
        fused_reranked, degraded, rerank_scores = await self._apply_rerank(
            query,
            fused,
            limit,
            degraded,
            window=max(assemble_mult * limit, assemble_min),
            top_n=int(setting_or_default(self._settings, "rerank_top_n")),
        )
        fused = fused_reranked
        window_f = max(assemble_mult * limit, assemble_min)
        expand = (
            setting_or_default(self._settings, "retriever_expand_window")
            if expand_window is None
            else expand_window
        )
        radius = (
            setting_or_default(self._settings, "retriever_window_radius")
            if window_radius is None
            else window_radius
        )
        results = self._postprocess(
            query,
            fused[:window_f],
            fts_ids,
            vec_ids,
            expand_window=bool(expand),
            window_radius=int(radius),
            rerank_scores=rerank_scores,
        )
        return results[:limit], degraded

    def _rrf_scores(self, fts_ids: list[int], vec_ids: list[int]) -> dict[int, float]:
        from libref.config import setting_or_default

        k = int(setting_or_default(self._settings, "rrf_k"))
        w_fts = float(setting_or_default(self._settings, "rrf_w_fts"))
        w_vec = float(setting_or_default(self._settings, "rrf_w_vec"))
        rank_fts = {doc: i + 1 for i, doc in enumerate(fts_ids)}
        rank_vec = {doc: i + 1 for i, doc in enumerate(vec_ids)}
        scored: dict[int, float] = {}
        for doc in list(rank_fts) + [d for d in rank_vec if d not in rank_fts]:
            s = 0.0
            if doc in rank_fts:
                s += w_fts / (k + rank_fts[doc])
            if doc in rank_vec:
                s += w_vec / (k + rank_vec[doc])
            scored[doc] = s
        return scored

    def _rrf(self, fts_ids: list[int], vec_ids: list[int]) -> list[int]:
        scored = self._rrf_scores(fts_ids, vec_ids)
        return sorted(scored, key=lambda d: scored[d], reverse=True)

    async def _apply_rerank(
        self,
        query: str,
        fused: list[int],
        limit: int,
        degraded: bool,
        *,
        window: int,
        top_n: int | None,
    ) -> tuple[list[int], bool, dict[int, float]]:
        from libref.config import setting_or_default

        if self._rerank is None:
            return fused, degraded, {}
        if top_n is not None:
            window = min(window, int(top_n))
        head, tail = fused[:window], fused[window:]
        if not head:
            return fused, degraded, {}
        radius = int(setting_or_default(self._settings, "snippet_radius"))
        cap = int(setting_or_default(self._settings, "snippet_cap"))
        details = _fetch_details(
            self._conn, head, query, snippet_radius=radius, snippet_cap=cap
        )
        texts = [details[d]["content"] for d in head if d in details]
        if len(texts) != len(head):
            return fused, degraded, {}
        try:
            scored = await self._rerank(query, texts, len(head))
        except Exception:  # noqa: BLE001 -- rerank failure marks degraded, keep fused order
            return fused, True, {}
        order = sorted(scored, key=lambda kv: kv[1], reverse=True)
        pos_of = {idx: rank for rank, (idx, _) in enumerate(order)}
        head_sorted = sorted(range(len(head)), key=lambda i: pos_of.get(i, len(order)))
        rerank_scores = {
            head[idx]: float(score) for idx, score in scored if 0 <= idx < len(head)
        }
        return [head[i] for i in head_sorted] + tail, degraded, rerank_scores

    def _assemble(
        self,
        query: str,
        fused: list[int],
        fts_ids: list[int],
        vec_ids: list[int],
        *,
        rerank_scores: dict[int, float] | None = None,
    ) -> list[dict[str, Any]]:
        from libref.config import setting_or_default

        radius = int(setting_or_default(self._settings, "snippet_radius"))
        cap = int(setting_or_default(self._settings, "snippet_cap"))
        details = _fetch_details(
            self._conn, fused, query, snippet_radius=radius, snippet_cap=cap
        )
        fts_set, vec_set = set(fts_ids), set(vec_ids)
        alpha = float(setting_or_default(self._settings, "freq_alpha"))
        cluster_alpha = float(setting_or_default(self._settings, "cluster_alpha"))
        rrf = self._rrf_scores(fts_ids, vec_ids)
        vids = sorted({details[d]["version_id"] for d in fused if d in details})
        patterns = _pattern_supports(self._conn, vids)
        clusters = _cluster_sets(self._conn, vids)
        scored: list[tuple[float, int]] = []
        for doc in fused:
            info = details.get(doc)
            if info is None:
                continue
            if rerank_scores:
                base = rerank_scores.get(doc, rrf.get(doc, 0.0))
            else:
                base = rrf.get(doc, 0.0)
            plist = patterns.get(info["version_id"], [])
            have_entities = list(info.get("entities", []) or [])
            if have_entities and alpha > 0:
                bonus = support_for_entities(have_entities, plist)
            else:
                bonus = (
                    support_for_content(info["content"], plist) if alpha > 0 else 0.0
                )
            cluster_bonus = 0.0
            clist = clusters.get(info["version_id"], [])
            if clist and cluster_alpha > 0:
                if have_entities:
                    for members in clist:
                        if cluster_hit_exact(have_entities, members):
                            cluster_bonus = 1.0
                            break
                else:
                    content = info["content"] or ""
                    for members in clist:
                        if cluster_hit(content, members):
                            cluster_bonus = 1.0
                            break
            final = base * (1.0 + alpha * bonus + cluster_alpha * cluster_bonus)
            scored.append((final, doc))
        scored.sort(key=lambda kv: kv[0], reverse=True)
        out: list[dict[str, Any]] = []
        for final, doc in scored:
            info = details[doc]
            sources = []
            if doc in fts_set:
                sources.append("fts")
            if doc in vec_set:
                sources.append("vec")
            out.append(
                {
                    "id": info["id"],
                    "url": info["url"],
                    "score": final,
                    "snippet": info["snippet"],
                    "title": info["title"],
                    "path": info["path"],
                    "library": info["library"],
                    "version": info["version"],
                    "sources": sources,
                    "chunk_path": info.get("chunk_path", "embedding"),
                    "source_group_id": info.get("source_group_id", ""),
                    "chunk_id": info.get("chunk_id", ""),
                    "page_id": info.get("page_id"),
                    "sort_order": info.get("sort_order"),
                    "span_kind": info.get("span_kind", "document_range"),
                    "merged_from": list(info.get("merged_from", []) or []),
                    "overlap_from": info.get("overlap_from"),
                    "overlap_text": str(info.get("overlap_text", "") or ""),
                    "chunk_index": int(info.get("chunk_index", 0) or 0),
                    "total_chunks": max(1, int(info.get("total_chunks", 1) or 1)),
                    "keywords": list(info.get("keywords", []) or []),
                    "content_type": info.get("content_type", "text/markdown"),
                    "entities": list(info.get("entities", []) or []),
                }
            )
        return out

    def _postprocess(
        self,
        query: str,
        fused: list[int],
        fts_ids: list[int],
        vec_ids: list[int],
        *,
        expand_window: bool,
        window_radius: int,
        rerank_scores: dict[int, float] | None = None,
    ) -> list[dict[str, Any]]:
        """Post-processing chain: assembly runs in order; on window-expansion failure the assembled order is kept."""
        results = self._assemble(
            query, fused, fts_ids, vec_ids, rerank_scores=rerank_scores
        )
        return self._expand_windows(
            results, enabled=expand_window, radius=window_radius
        )

    def _expand_windows(
        self, results: list[dict[str, Any]], *, enabled: bool, radius: int
    ) -> list[dict[str, Any]]:
        """Window-expansion post-processing: prepend/append neighboring chunks by the configured radius; on failure keep the original order."""
        from libref.config import setting_or_default

        if not enabled or not results:
            return results
        radius = max(0, int(radius))
        if radius <= 0:
            return results
        preview_cap = int(setting_or_default(self._settings, "window_preview_cap"))
        try:
            from libref.store.document_store import DocumentStore

            store = DocumentStore(self._conn)
            by_id = {
                int(r["id"]): r
                for r in self._conn.execute(
                    f"SELECT id, page_id, sort_order FROM documents WHERE id IN ({','.join('?' for _ in results)})",
                    [r["id"] for r in results],
                ).fetchall()
            }
            expanded: list[dict[str, Any]] = []
            for item in results:
                loc = by_id.get(int(item["id"]))
                if loc is None:
                    expanded.append(item)
                    continue
                hit_path = str(item.get("chunk_path", "embedding") or "embedding")
                window = store.get_window(
                    int(loc[1]), int(loc[2]), radius, chunk_path=hit_path
                )
                if len(window) > 1:
                    item = dict(item)
                    item["window"] = [
                        {
                            "id": w["id"],
                            "sort_order": w["sort_order"],
                            "chunk_id": str((w.get("metadata") or {}).get("chunk_id", "") or ""),
                            "source_group_id": str((w.get("metadata") or {}).get("source_group_id", "") or ""),
                            "content": truncate_material(w["content"], preview_cap),
                        }
                        for w in window
                    ]
                expanded.append(item)
            return expanded
        except Exception:  # noqa: BLE001 -- expansion failure keeps the original results
            return results


def build_retriever(settings, conn, *, vec_store=None, embed=None, rerank=None):
    """Build a hybrid retriever from settings; degrades to FTS-only when vectors are missing."""
    return HybridRetriever(
        conn, settings, vec_store=vec_store, embed=embed, rerank=rerank
    )
