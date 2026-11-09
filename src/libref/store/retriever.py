"""混合检索编排，双路召回按排名融合；失败降级由调用方感知，不静默。"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

_CAMEL_BOUND_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _snake_variant(token: str) -> str:
    """查询词的下划线变体，相同则返回空串。"""
    snake = _CAMEL_BOUND_RE.sub("_", token).lower()
    return snake if snake and snake != token.lower() else ""


def _safe_fts_query(query: str) -> str:
    """把用户输入转成无语法错的全文查询，无有效词元时返回空串。"""
    latin = re.findall(r"[A-Za-z0-9_]+", query or "")
    cjk = re.findall(r"[\u4e00-\u9fff]+", query or "")
    seen: set[str] = set()
    parts: list[str] = []
    for token in latin:
        lowered = token.lower()
        if lowered and lowered not in seen:
            seen.add(lowered)
            parts.append(f'"{token}"')
        snake = _snake_variant(token)
        if snake and snake not in seen:
            seen.add(snake)
            parts.append(f'"{snake}"')
    for token in cjk:
        if token not in seen:
            seen.add(token)
            parts.append(f'"{token}"')
    return " OR ".join(parts)


def _make_snippet(content: str | None, query: str, *, radius: int, cap: int) -> str:
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
    start = max(0, pos - radius)
    end = min(len(text), pos + radius)
    line_start = text.rfind("\n", 0, start)
    start = line_start + 1 if line_start >= 0 else 0
    line_end = text.find("\n", end)
    end = line_end if line_end >= 0 else len(text)
    if end - start > cap:
        end = start + cap
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
    *,
    weights: tuple[float, float, float, float],
) -> list[int]:
    safe = _safe_fts_query(query)
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


_IDENT_RE = re.compile(r"[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+")
_TOKEN_RE = re.compile(r"[A-Za-z_][\w]*")


def _content_idents(content: str) -> tuple[set[str], set[str]]:
    """抽取块内点分标识全名与词元集合，命中只认词元边界精确匹配。"""
    full = set(_IDENT_RE.findall(content or ""))
    toks = set(_TOKEN_RE.findall(content or ""))
    return full, toks


def _name_hit(name: str, full: set[str], toks: set[str]) -> bool:
    if name in full or name in toks:
        return True
    short = name.rsplit(".", 1)[-1]
    return short in toks


def _support_for_content(content: str, plist: list[tuple[list[str], int]]) -> float:
    """块归属取命中模式的最大支持度并按版本内最大值归一，无命中返回零。"""
    if not plist:
        return 0.0
    ceiling = max(s for _, s in plist)
    if ceiling <= 0:
        return 0.0
    full, toks = _content_idents(content)
    hit = max(
        (s for names, s in plist if any(_name_hit(n, full, toks) for n in names)),
        default=0,
    )
    return hit / ceiling


def _cluster_hit(content: str, members: set[str]) -> bool:
    full, toks = _content_idents(content)
    return sum(1 for n in members if _name_hit(n, full, toks)) >= 2


def _fetch_details(
    conn: sqlite3.Connection, doc_ids: list[int], query: str,
    *, snippet_radius: int, snippet_cap: int,
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
            "snippet": _make_snippet(r[2], query, radius=snippet_radius, cap=snippet_cap),
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
            "keywords": list(meta.get("keywords", []) or []),
            "content_type": meta.get("content_type", "text/markdown"),
            "page_id": int(r[9]),
            "sort_order": int(r[10]),
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
        expand_window: bool | None = None,
        window_radius: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """混合检索，返回结果列表与降级标记。"""
        from libref.config import setting_or_default

        if not query.strip():
            raise ValueError("query is empty")
        if limit <= 0:
            raise ValueError("limit must be positive")
        degraded = False
        candidate_floor = int(setting_or_default(self._settings, "rerank_candidate_limit"))
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
        assemble_mult = int(setting_or_default(self._settings, "assemble_multiplier"))
        assemble_min = int(setting_or_default(self._settings, "assemble_min"))
        reranked = await self._apply_rerank(
            query, fused, limit, degraded,
            window=max(assemble_mult * limit, assemble_min),
            top_n=int(setting_or_default(self._settings, "rerank_top_n")),
        )
        fused, degraded = reranked
        window_f = max(assemble_mult * limit, assemble_min)
        expand = setting_or_default(self._settings, "retriever_expand_window") if expand_window is None else expand_window
        radius = setting_or_default(self._settings, "retriever_window_radius") if window_radius is None else window_radius
        results = self._postprocess(
            query, fused[:window_f], fts_ids, vec_ids,
            expand_window=bool(expand), window_radius=int(radius),
        )
        return results[:limit], degraded

    def _rrf_scores(
        self, fts_ids: list[int], vec_ids: list[int]
    ) -> dict[int, float]:
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
        self, query: str, fused: list[int], limit: int, degraded: bool,
        *, window: int, top_n: int | None,
    ) -> tuple[list[int], bool]:
        from libref.config import setting_or_default

        if self._rerank is None:
            return fused, degraded
        if top_n is not None:
            window = min(window, int(top_n))
        head, tail = fused[:window], fused[window:]
        if not head:
            return fused, degraded
        radius = int(setting_or_default(self._settings, "snippet_radius"))
        cap = int(setting_or_default(self._settings, "snippet_cap"))
        details = _fetch_details(self._conn, head, query, snippet_radius=radius, snippet_cap=cap)
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
        from libref.config import setting_or_default

        radius = int(setting_or_default(self._settings, "snippet_radius"))
        cap = int(setting_or_default(self._settings, "snippet_cap"))
        details = _fetch_details(self._conn, fused, query, snippet_radius=radius, snippet_cap=cap)
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
            base = rrf.get(doc, 0.0)
            plist = patterns.get(info["version_id"], [])
            bonus = _support_for_content(info["content"], plist) if alpha > 0 else 0.0
            cluster_bonus = 0.0
            clist = clusters.get(info["version_id"], [])
            if clist and cluster_alpha > 0:
                content = info["content"] or ""
                for members in clist:
                    if _cluster_hit(content, members):
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
                    "keywords": list(info.get("keywords", []) or []),
                    "content_type": info.get("content_type", "text/markdown"),
                }
            )
        return out

    def _postprocess(self, query: str, fused: list[int], fts_ids: list[int], vec_ids: list[int], *, expand_window: bool, window_radius: int) -> list[dict[str, Any]]:
        """后处理链：装配按序执行，窗口还原失败保留装配顺序。"""
        results = self._assemble(query, fused, fts_ids, vec_ids)
        return self._expand_windows(results, enabled=expand_window, radius=window_radius)


    def _expand_windows(self, results: list[dict[str, Any]], *, enabled: bool, radius: int) -> list[dict[str, Any]]:
        """窗口还原后处理：按配置半径取前后块拼入片段，失败即保留原序。"""
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
            by_id = {int(r["id"]): r for r in self._conn.execute(
                "SELECT id, page_id, sort_order FROM documents WHERE id IN (%s)" % ",".join("?" for _ in results),
                [r["id"] for r in results],
            ).fetchall()}
            expanded: list[dict[str, Any]] = []
            for item in results:
                loc = by_id.get(int(item["id"]))
                if loc is None:
                    expanded.append(item)
                    continue
                hit_path = str(item.get("chunk_path", "embedding") or "embedding")
                window = store.get_window(int(loc[1]), int(loc[2]), radius, chunk_path=hit_path)
                if len(window) > 1:
                    item = dict(item)
                    item["window"] = [
                        {"id": w["id"], "sort_order": w["sort_order"], "content": (w["content"] or "")[:preview_cap]}
                        for w in window
                    ]
                expanded.append(item)
            return expanded
        except Exception:  # noqa: BLE001 —— 扩展失败保留原结果
            return results


def build_retriever(settings, conn, *, vec_store=None, embed=None, rerank=None):
    """按配置构造混合检索器，缺向量时退化为纯全文。"""
    return HybridRetriever(conn, settings, vec_store=vec_store,
                           embed=embed, rerank=rerank)
