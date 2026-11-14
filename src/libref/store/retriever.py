"""Storage-level retrieval primitives and hybrid fusion over SQLite and Qdrant.

SQL access stays here. String shaping lives in libref.text, mining
weighting lives in libref.mining.scoring.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

from libref.mining.idiom import reuse_diversity
from libref.mining.scoring import (
    capped_alpha,
    cluster_hit,
    cluster_hit_exact,
    cluster_hit_weak,
    cluster_hit_weak_exact,
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
    # Newest ingested first: scoped recall fans out over versions until the cap truncates,
    # and the truncated tail must be the oldest versions rather than the freshest ones.
    rows = conn.execute(
        f"SELECT v.id FROM versions v JOIN libraries l ON l.id = v.library_id {where}"
        " ORDER BY v.id DESC",
        params,
    ).fetchall()
    return [int(r[0]) for r in rows]


def _bm25_expr(weights: tuple[float, float, float, float]) -> str:
    return (
        f"bm25(documents_fts, {weights[0]}, {weights[1]}, {weights[2]}, {weights[3]})"
    )


def fts_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    library: str | None = None,
    version: str | None = None,
    ecosystem: str | None = None,
    weights: tuple[float, float, float, float],
    budget: int,
) -> list[tuple[int, float]]:
    """Single shared full-text read path, returning document ids with raw scores.

    Scores are raw match values in ascending order, best first. Without filters
    the database sorts and truncates. With filters the scope resolves to version
    ids first and only scoped candidates are ranked, so large unrelated versions
    never enter the sort. An empty scope yields nothing.
    """
    safe = safe_fts_query(query)
    if not safe or budget <= 0:
        return []
    scoped = library is not None or version is not None or ecosystem is not None
    if not scoped:
        rows = conn.execute(
            f"SELECT documents_fts.rowid, {_bm25_expr(weights)} FROM documents_fts "
            "WHERE documents_fts MATCH ? "
            f"ORDER BY {_bm25_expr(weights)} LIMIT ?",
            [safe, budget],
        ).fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]
    vids = _version_ids(conn, library, version, ecosystem)
    if not vids:
        return []
    marks = ",".join("?" for _ in vids)
    # The scope predicate is a correlated EXISTS rather than `rowid IN (subquery)`:
    # SQLite materialises an IN subquery into an unindexed list and probes it once per
    # index row, which turns a scoped search over a large corpus quadratic (seconds per
    # query instead of milliseconds). The EXISTS form is an indexed primary-key probe.
    rows = conn.execute(
        f"SELECT documents_fts.rowid, {_bm25_expr(weights)} FROM documents_fts "
        "WHERE documents_fts MATCH ? "
        "AND EXISTS (SELECT 1 FROM documents d WHERE d.id = documents_fts.rowid "
        f"AND d.version_id IN ({marks})) "
        f"ORDER BY {_bm25_expr(weights)} LIMIT ?",
        [safe, *vids, budget],
    ).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows]


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
    return [
        doc_id
        for doc_id, _ in fts_search(
            conn,
            query,
            library=library,
            version=version,
            ecosystem=ecosystem,
            weights=weights,
            budget=limit,
        )
    ]


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
    """Cross-path alignment key: document section first, chunk id next, entity as fallback.

    Section-first keeps twin chunks from both lanes together while unrelated
    sections sharing one entity no longer collapse each other.
    """
    data = meta or {}
    group = str(data.get("source_group_id") or "").strip()
    if group:
        return f"s:{url or ''}\x00{group}"
    chunk_id = str(data.get("chunk_id") or "").strip()
    if chunk_id:
        return f"c:{chunk_id}"
    entities = data.get("entities") or []
    if entities:
        first = str(entities[0]).strip()
        if first:
            return f"e:{first}"
    return f"c:{doc_id}"


def _collapse_duplicates(conn: sqlite3.Connection, fused: list[int]) -> list[int]:
    """Drop the auxiliary full-text twin of a section part, then cap each source group at two chunks.

    Both chunk lanes index the same sections: the auxiliary lane only exists for lexical
    matching, so its copy never becomes user-facing material while the full-body twin is
    recalled. The per-group cap stays because merged sections legitimately split into parts.
    """
    if len(fused) < 2:
        return fused
    marks = ",".join("?" for _ in fused)
    rows = conn.execute(
        f"SELECT id, url, metadata FROM documents WHERE id IN ({marks})", fused
    ).fetchall()
    groups: dict[int, str] = {}
    lanes: dict[int, str] = {}
    parts: dict[int, str] = {}
    full_body_parts: set[str] = set()
    for doc_id, url, raw in rows:
        try:
            meta = json.loads(raw) if raw else {}
        except ValueError:
            meta = {}
        key = alignment_key(meta, url, int(doc_id))
        lane = str(meta.get("chunk_path") or "embedding")
        part = str(meta.get("chunk_index", 0) or 0)
        groups[int(doc_id)] = key
        lanes[int(doc_id)] = lane
        parts[int(doc_id)] = part
        if lane != "bm25":
            full_body_parts.add(f"{key}\x00{part}")
    counts: dict[str, int] = {}
    out: list[int] = []
    for doc_id in fused:
        group_key = groups.get(doc_id)
        if group_key is None:
            out.append(doc_id)
            continue
        if (
            lanes[doc_id] == "bm25"
            and f"{group_key}\x00{parts[doc_id]}" in full_body_parts
        ):
            continue
        seen = counts.get(group_key, 0)
        if seen >= 2:
            continue
        counts[group_key] = seen + 1
        out.append(doc_id)
    return out


def _cross_lingual_miss(query: str, fts_ids: list[int], vec_ids: list[int]) -> bool:
    """A CJK query whose lexical lane matched nothing while the vector lane did.

    The full-text lane matches literal terms, so a Chinese query over an English
    corpus cannot match there; reporting it as a failure to find evidence would
    be wrong. The vector lane carries such queries on its own, which the fused
    order already reflects: with no lexical ids the fusion leaves vector ranks
    untouched.
    """
    if fts_ids or not vec_ids:
        return False
    from libref.text.cjk import CJK_RUN_RE

    return bool(CJK_RUN_RE.search(query or ""))


def _pattern_supports(
    conn: sqlite3.Connection,
    version_ids: list[int],
    wanted: set[str] | None = None,
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
        if wanted and not set(names).intersection(wanted):
            continue
        out.setdefault(int(vid), []).append((names, int(support)))
    for vid in list(out):
        out[vid].sort(key=lambda item: (-item[1], item[0]))
        del out[vid][500:]
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

    if not vids:
        return [], False
    model = setting_or_default(settings, "embedding_model")
    dim = int(setting_or_default(settings, "embedding_dim"))
    marks = ",".join("?" for _ in vids)
    known = {
        int(r[0]): (r[1], int(r[2]))
        for r in conn.execute(
            f"SELECT version_id, model, dim FROM vec_version_meta WHERE version_id IN ({marks})",
            [int(v) for v in vids],
        ).fetchall()
    }
    current: list[int] = []
    skipped = False
    for vid in vids:
        record = known.get(int(vid))
        if record is None:
            current.append(int(vid))
        elif (record[0], record[1]) != (model, dim):
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
            self._conn, query, library, version, ecosystem, need, weights=weights
        )
        vec_ids: list[int] = []
        vids = _version_ids(self._conn, library, version, ecosystem)
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
                                for doc_id, _ in self._vec.search(
                                    query_vec, k=need, version_id=vids[0]
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

    def _status(
        self,
        degraded: bool,
        reasons: list[str],
        fts_ids: list[int],
        vec_ids: list[int],
        fused_order: list[int],
        rerank_applied: bool,
        answerability: dict[str, Any],
        cross_lingual: bool,
    ) -> dict[str, Any]:
        return {
            "degraded": bool(degraded),
            "reasons": list(dict.fromkeys(reasons)),
            "fts_count": len(fts_ids),
            "vec_count": len(vec_ids),
            "fused_order": list(fused_order),
            "rerank_applied": bool(rerank_applied),
            "cross_lingual": bool(cross_lingual),
            **answerability,
        }

    async def _apply_rerank(
        self,
        query: str,
        fused: list[int],
        *,
        window: int,
        top_n: int | None,
    ) -> tuple[list[int], dict[int, float], bool]:
        """Rerank the recall head; returns the reordered ids, the score map, and the failure flag."""
        from libref.config import setting_or_default

        if self._rerank is None:
            return fused, {}, False
        if top_n is not None:
            window = min(window, int(top_n))
        head, tail = fused[:window], fused[window:]
        if not head:
            return fused, {}, False
        radius = int(setting_or_default(self._settings, "snippet_radius"))
        cap = int(setting_or_default(self._settings, "snippet_cap"))
        details = _fetch_details(
            self._conn, head, query, snippet_radius=radius, snippet_cap=cap
        )
        head = [d for d in head if d in details]
        if not head:
            return tail, {}, False
        link_cap = int(setting_or_default(self._settings, "rerank_truncate"))
        total_cap = int(setting_or_default(self._settings, "material_limit"))
        link_cap = min(link_cap, total_cap)
        texts = [truncate_material(details[d]["content"], link_cap) for d in head]
        try:
            scored = await self._rerank(query, texts, len(head))
        except Exception:  # noqa: BLE001 -- rerank failure keeps fused order and is reported by the caller
            return fused, {}, True
        order = sorted(scored, key=lambda kv: kv[1], reverse=True)
        pos_of = {idx: rank for rank, (idx, _) in enumerate(order)}
        head_sorted = sorted(range(len(head)), key=lambda i: pos_of.get(i, len(order)))
        rerank_scores = {
            head[idx]: float(score) for idx, score in scored if 0 <= idx < len(head)
        }
        return [head[i] for i in head_sorted] + tail, rerank_scores, False

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
        raw_alpha = float(setting_or_default(self._settings, "freq_alpha"))
        cluster_alpha = float(setting_or_default(self._settings, "cluster_alpha"))
        rrf = self._rrf_scores(fts_ids, vec_ids)
        vids = sorted({details[d]["version_id"] for d in fused if d in details})
        wanted: set[str] = set()
        if raw_alpha > 0:
            for doc in fused:
                info = details.get(doc)
                if info is None:
                    continue
                for entity in info.get("entities", []) or []:
                    if entity:
                        wanted.add(str(entity))
        patterns = _pattern_supports(self._conn, vids, wanted) if raw_alpha > 0 else {}
        clusters = _cluster_sets(self._conn, vids) if cluster_alpha > 0 else {}
        health_rows = [{"support": s} for plist in patterns.values() for _, s in plist]
        health = reuse_diversity(
            health_rows,
            [{"members": members} for sets in clusters.values() for members in sets],
        )
        alpha = capped_alpha(raw_alpha, float(health.get("reusability", 0.0)))
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
                    if cluster_bonus == 0.0:
                        for members in clist:
                            if cluster_hit_weak_exact(have_entities, members):
                                cluster_bonus = 0.3
                                break
                else:
                    content = info["content"] or ""
                    for members in clist:
                        if cluster_hit(content, members):
                            cluster_bonus = 1.0
                            break
                    if cluster_bonus == 0.0:
                        for members in clist:
                            if cluster_hit_weak(content, members):
                                cluster_bonus = 0.3
                                break
            final = base * (1.0 + alpha * bonus + cluster_alpha * cluster_bonus)
            scored.append((final, doc))
        scored.sort(key=lambda kv: kv[0], reverse=True)
        out: list[dict[str, Any]] = []
        seen_excerpts: set[tuple[str, str]] = set()
        for final, doc in scored:
            info = details[doc]
            excerpt = (str(info["url"] or ""), str(info["snippet"] or ""))
            if excerpt in seen_excerpts:
                continue
            seen_excerpts.add(excerpt)
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
    ) -> tuple[list[dict[str, Any]], bool]:
        """Post-processing chain: assembly runs in order; on window-expansion failure the assembled order is kept."""
        results = self._assemble(
            query,
            fused,
            fts_ids,
            vec_ids,
            rerank_scores=rerank_scores,
        )
        return self._expand_windows(
            results, enabled=expand_window, radius=window_radius
        )

    def _expand_windows(
        self, results: list[dict[str, Any]], *, enabled: bool, radius: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Window-expansion post-processing: prepend/append neighboring chunks by the configured radius; on failure keep the original order."""
        from libref.config import setting_or_default

        if not enabled or not results:
            return results, True
        radius = max(0, int(radius))
        if radius <= 0:
            return results, True
        preview_cap = int(setting_or_default(self._settings, "window_preview_cap"))
        try:
            locs = {
                int(r[0]): (int(r[1]), int(r[2]), str(r[3] or "embedding"))
                for r in self._conn.execute(
                    f"SELECT d.id, d.page_id, d.sort_order,"
                    f" COALESCE(json_extract(d.metadata, '$.chunk_path'), 'embedding')"
                    f" FROM documents d WHERE d.id IN ({','.join('?' for _ in results)})",
                    [r["id"] for r in results],
                ).fetchall()
            }
            by_page: dict[int, list[int]] = {}
            for doc_id, (page_id, _order, _lane) in locs.items():
                by_page.setdefault(page_id, []).append(doc_id)
            windows: dict[int, list[dict[str, Any]]] = {}
            for page_id, doc_ids in by_page.items():
                orders = sorted(locs[d][1] for d in doc_ids)
                lanes = {locs[d][2] for d in doc_ids}
                lo, hi = orders[0] - radius, orders[-1] + radius
                marks = ",".join("?" for _ in lanes)
                rows = self._conn.execute(
                    "SELECT id, url, content, metadata, sort_order FROM documents "
                    "WHERE page_id=? AND sort_order BETWEEN ? AND ? "
                    f"AND COALESCE(json_extract(metadata, '$.chunk_path'), 'embedding') IN ({marks}) "
                    "ORDER BY sort_order",
                    (page_id, lo, hi, *sorted(lanes)),
                ).fetchall()
                import json as _json

                by_order: dict[int, list[dict[str, Any]]] = {}
                for wid, wurl, wcontent, wmeta, worder in rows:
                    try:
                        parsed = _json.loads(wmeta) if wmeta else {}
                    except ValueError:
                        parsed = {}
                    by_order.setdefault(int(worder), []).append(
                        {
                            "id": int(wid),
                            "url": wurl,
                            "content": wcontent,
                            "metadata": parsed,
                            "sort_order": int(worder),
                        }
                    )
                for doc_id in doc_ids:
                    order = locs[doc_id][1]
                    lane = locs[doc_id][2]
                    picked = [
                        w
                        for o in range(order - radius, order + radius + 1)
                        for w in by_order.get(o, [])
                        if str(
                            (w["metadata"] or {}).get("chunk_path", "embedding")
                            or "embedding"
                        )
                        == lane
                    ]
                    windows[doc_id] = picked
            expanded: list[dict[str, Any]] = []
            covered: set[int] = set()
            for item in results:
                doc_id = int(item["id"])
                loc = locs.get(doc_id)
                if loc is not None:
                    page_id, order, lane = loc
                    for other in results:
                        other_id = int(other["id"])
                        if other_id == doc_id:
                            continue
                        other_loc = locs.get(other_id)
                        if other_loc is None:
                            continue
                        if (
                            other_loc[0] == page_id
                            and other_loc[2] == lane
                            and abs(other_loc[1] - order) <= radius
                        ):
                            covered.add(doc_id)
                            break
                window = windows.get(int(item["id"]), [])
                if len(window) > 1 and int(item["id"]) not in covered:
                    item = dict(item)
                    item["window"] = [
                        {
                            "id": w["id"],
                            "sort_order": w["sort_order"],
                            "chunk_id": str(
                                (w.get("metadata") or {}).get("chunk_id", "") or ""
                            ),
                            "source_group_id": str(
                                (w.get("metadata") or {}).get("source_group_id", "")
                                or ""
                            ),
                            "content": truncate_material(w["content"], preview_cap),
                        }
                        for w in window
                    ]
                expanded.append(item)
            return expanded, True
        except Exception:  # noqa: BLE001 -- expansion failure keeps the original results
            return results, False


def build_retriever(settings, conn, *, vec_store=None, embed=None, rerank=None):
    """Build a hybrid retriever from settings; degrades to FTS-only when vectors are missing."""
    return HybridRetriever(
        conn, settings, vec_store=vec_store, embed=embed, rerank=rerank
    )
