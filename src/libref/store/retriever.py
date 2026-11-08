"""混合检索编排，双路召回按排名融合；失败降级由调用方感知，不静默。"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

_SNIPPET_RADIUS = 60


def _make_snippet(content: str | None, query: str) -> str:
    text = (content or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    pos = -1
    for token in re.findall(r"[\w-]+", query.lower()):
        idx = lowered.find(token)
        if idx >= 0 and (pos < 0 or idx < pos):
            pos = idx
    pos = max(pos, 0)
    start = max(0, pos - _SNIPPET_RADIUS)
    end = min(len(text), pos + _SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    window = text[start:end]
    for token in sorted(set(re.findall(r"[\w-]+", query)), key=len, reverse=True):
        pattern = re.compile(re.escape(token), re.IGNORECASE)
        window = pattern.sub(lambda m: f">>>{m.group(0)}<<<", window)
    return f"{prefix}{window}{suffix}"


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
        "SELECT v.id FROM versions v JOIN libraries l ON l.id = v.library_id "
        f"{where}",
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
) -> list[int]:
    filters = []
    params: list[Any] = [query]
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
        "ORDER BY bm25(documents_fts, 1.0, 5.0, 1.0, 3.0) LIMIT ?",
        params,
    ).fetchall()
    return [int(r[0]) for r in rows]


def _fetch_details(
    conn: sqlite3.Connection, doc_ids: list[int], query: str
) -> dict[int, dict[str, Any]]:
    if not doc_ids:
        return {}
    marks = ",".join("?" for _ in doc_ids)
    rows = conn.execute(
        "SELECT d.id, d.url, d.content, json_extract(d.metadata, '$.title'), "
        "json_extract(d.metadata, '$.path'), l.name, v.version, d.version_id, "
        "d.metadata "
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
            "snippet": _make_snippet(r[2], query),
            "title": r[3],
            "path": r[4],
            "library": r[5],
            "version": r[6],
            "version_id": int(r[7]),
            "content": r[2] or "",
            "chunk_path": meta.get("chunk_path", "embedding"),
            "source_group_id": meta.get("source_group_id", ""),
        }
    return out


def _collapse_by_group(
    conn: sqlite3.Connection, fused: list[int]
) -> list[int]:
    """同源分组只保留排序最前的块，两路孪生块合并为一次命中。"""
    if len(fused) < 2:
        return fused
    marks = ",".join("?" for _ in fused)
    rows = conn.execute(
        f"SELECT id, metadata FROM documents WHERE id IN ({marks})", fused
    ).fetchall()
    groups: dict[int, str] = {}
    for doc_id, raw in rows:
        try:
            meta = json.loads(raw) if raw else {}
        except ValueError:
            meta = {}
        groups[int(doc_id)] = meta.get("source_group_id") or f"doc:{doc_id}"
    seen: set[str] = set()
    out: list[int] = []
    for doc_id in fused:
        key = groups.get(doc_id, f"doc:{doc_id}")
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


class HybridRetriever:
    """双路召回与融合重排的编排位置，向量与重排失败只标记降级。"""

    def __init__(
        self, conn, settings=None, *, vec_store=None, embed=None, rerank=None
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._vec = vec_store if embed is not None else None
        self._embed = embed if vec_store is not None else None
        self._rerank = rerank

    async def search_async(
        self,
        query: str,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 8,
    ) -> tuple[list[dict[str, Any]], bool]:
        """混合检索，返回结果列表与降级标记。"""
        if not query.strip():
            raise ValueError("query is empty")
        if limit <= 0:
            raise ValueError("limit must be positive")
        degraded = False
        candidate_floor = int(getattr(self._settings, "rerank_candidate_limit", 30))
        need = max(3 * limit, candidate_floor)
        fts_ids = _fts_candidates(
            self._conn, query, library, version, ecosystem, need
        )
        vec_ids: list[int] = []
        vids = _version_ids(self._conn, library, version, ecosystem)
        single_version = vids[0] if len(vids) == 1 else None
        if self._vec is not None and self._embed is not None and single_version is not None:
            try:
                [query_vec] = await self._embed([query])
                vec_ids = [
                    doc_id
                    for doc_id, _ in self._vec.search(
                        query_vec, k=need, version_id=single_version
                    )
                ]
            except Exception:  # noqa: BLE001 —— 向量失败标记降级，走纯全文
                degraded = True
                vec_ids = []
        elif self._vec is not None and self._embed is not None:
            degraded = True
        fused = self._rrf(fts_ids, vec_ids)
        fused = _collapse_by_group(self._conn, fused)
        if not fused:
            return [], degraded
        reranked = await self._apply_rerank(query, fused, limit, degraded)
        fused, degraded = reranked
        window_f = max(2 * limit, 10)
        results = self._assemble(query, fused[:window_f], fts_ids, vec_ids)
        return results[:limit], degraded

    def _rrf_scores(
        self, fts_ids: list[int], vec_ids: list[int]
    ) -> dict[int, float]:
        k = int(getattr(self._settings, "rrf_k", 20))
        w_fts = float(getattr(self._settings, "rrf_w_fts", 1.0))
        w_vec = float(getattr(self._settings, "rrf_w_vec", 0.5))
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
        self, query: str, fused: list[int], limit: int, degraded: bool
    ) -> tuple[list[int], bool]:
        if self._rerank is None:
            return fused, degraded
        window = max(2 * limit, 10)
        top_n = getattr(self._settings, "rerank_top_n", None)
        if top_n is not None:
            window = min(window, int(top_n))
        head, tail = fused[:window], fused[window:]
        if not head:
            return fused, degraded
        details = _fetch_details(self._conn, head, query)
        texts = [details[d]["content"] for d in head if d in details]
        if len(texts) != len(head):
            return fused, degraded
        try:
            scored = await self._rerank(query, texts, len(head))
        except Exception:  # noqa: BLE001 —— 重排失败标记降级，保留融合排序
            return fused, True
        order = sorted(scored, key=lambda kv: kv[1], reverse=True)
        pos_of = {idx: rank for rank, (idx, _) in enumerate(order)}
        head_sorted = sorted(
            range(len(head)), key=lambda i: pos_of.get(i, len(order))
        )
        return [head[i] for i in head_sorted] + tail, degraded

    def _assemble(
        self, query: str, fused: list[int], fts_ids: list[int], vec_ids: list[int]
    ) -> list[dict[str, Any]]:
        details = _fetch_details(self._conn, fused, query)
        fts_set, vec_set = set(fts_ids), set(vec_ids)
        alpha = float(getattr(self._settings, "freq_alpha", 0.15))
        cluster_alpha = float(getattr(self._settings, "cluster_alpha", 0.05))
        rrf = self._rrf_scores(fts_ids, vec_ids)
        vids = sorted({details[d]["version_id"] for d in fused if d in details})
        patterns = _pattern_supports(self._conn, vids)
        clusters = _cluster_sets(self._conn, vids)
        scored: list[tuple[float, int]] = []
        for doc in fused:
            info = details.get(doc)
            if info is None:
                continue
            base = rrf.get(doc, 0.0)
            bonus = 0.0
            plist = patterns.get(info["version_id"], [])
            if plist and alpha > 0:
                ceiling = max(s for _, s in plist)
                if ceiling > 0:
                    hit = max(
                        (s for names, s in plist if any(n in (info["content"] or "") for n in names)),
                        default=0,
                    )
                    bonus = hit / ceiling
            cluster_bonus = 0.0
            clist = clusters.get(info["version_id"], [])
            if clist and cluster_alpha > 0:
                content = info["content"] or ""
                for members in clist:
                    hits = sum(1 for n in members if n in content)
                    if hits >= 2:
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
                }
            )
        return out


def build_retriever(settings, conn, *, vec_store=None, embed=None, rerank=None):
    """按配置构造混合检索器，缺向量时退化为纯全文。"""
    return HybridRetriever(conn, settings, vec_store=vec_store,
                           embed=embed, rerank=rerank)
