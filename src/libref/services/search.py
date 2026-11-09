"""检索服务：全文检索加页面读取加库版本列举。"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from libref.store.vec_store import build_embed_text


def _is_subsequence(pattern: list[str], names: list[str]) -> bool:
    pos = 0
    for want in pattern:
        while pos < len(names) and names[pos] != want:
            pos += 1
        if pos == len(names):
            return False
        pos += 1
    return True


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
    """围绕首个查询词命中位置截取上下文片段（替代 FTS5 snippet()）。"""
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
    # 命中的查询词用 >>> <<< 标记
    for token in sorted(set(re.findall(r"[\w-]+", query)), key=len, reverse=True):
        pattern = re.compile(re.escape(token), re.IGNORECASE)
        window = pattern.sub(lambda m: f">>>{m.group(0)}<<<", window)
    return f"{prefix}{window}{suffix}"


class SearchService:
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
        """全文检索，库名与版本号为可选过滤。

        外部内容表的 snippet 系列辅助函数在本库形态下不可用，
        片段在 Python 侧按查询词截取。列权重按信息密度给定：
        标题最高，路径次之，内容与地址为基准。
        """
        from libref.config import setting_or_default

        filters = []
        safe = _safe_fts_query(query)
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
        seen: set[tuple] = set()
        for r in rows:
            try:
                meta = json.loads(r[8]) if r[8] else {}
            except ValueError:
                meta = {}
            key = (r[1], meta.get("source_group_id") or f"doc:{r[0]}")
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "id": r[0],
                    "url": r[1],
                    "score": r[2],
                    "snippet": _make_snippet(r[3], query, radius=radius, cap=cap),
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
                    "keywords": list(meta.get("keywords", []) or []),
                    "content_type": meta.get("content_type", "text/markdown"),
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
        """混合检索，返回结果与降级标记；向量缺失时只走全文。"""
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

    async def reembed_library_async(
        self,
        library: str,
        version: str,
        ecosystem: str | None = None,
        *,
        settings=None,
        vec_store=None,
        embed=None,
    ) -> dict[str, int]:
        """按真源全量重灌整版本向量，返回文档数与写入数。"""
        settings = settings if settings is not None else self._settings
        vec = vec_store if vec_store is not None else self._vec
        fn = embed if embed is not None else self._embed
        if settings is None:
            raise ValueError("settings is required")
        if vec is None or fn is None:
            raise ValueError("vec_store and embed are required")
        vids = self._mining_version_ids(library, version, ecosystem)
        if len(vids) != 1:
            raise ValueError("library version must resolve to a single version")
        version_id = vids[0]
        vec.ensure_collection(
            self._conn,
            settings.embedding_model,
            settings.embedding_dim,
        )
        vec.delete_version(version_id)
        rows = self._conn.execute(
            "SELECT id, library_id, version_id, content, metadata "
            "FROM documents WHERE version_id=? ORDER BY id",
            (version_id,),
        ).fetchall()
        if not rows:
            return {"docs": 0, "upserted": 0}
        embeddable = []
        for r in rows:
            try:
                meta = json.loads(r[4]) if r[4] else {}
            except ValueError:
                meta = {}
            if meta.get("chunk_path", "embedding") == "bm25":
                continue
            embeddable.append(r)
        upserted = 0
        for i in range(0, len(embeddable), settings.embed_batch):
            batch = embeddable[i:i + settings.embed_batch]
            texts = []
            for r in batch:
                try:
                    meta = json.loads(r[4]) if r[4] else {}
                except ValueError:
                    meta = {}
                texts.append(build_embed_text(meta.get("title"), meta.get("path"), r[3], meta.get("keywords")))
            vecs = await fn(texts)
            if len(vecs) != len(batch):
                raise RuntimeError("embedding count mismatch")
            vec.upsert_batch([
                (r[0], r[1], r[2], v) for r, v in zip(batch, vecs)
            ])
            upserted += len(batch)
        return {"docs": len(rows), "upserted": upserted}

    def reembed_library(
        self,
        library: str,
        version: str,
        ecosystem: str | None = None,
        *,
        settings=None,
        vec_store=None,
        embed=None,
    ) -> dict[str, int]:
        """重灌的同步入口，供命令行直接调用。"""
        return asyncio.run(self.reembed_library_async(
            library, version, ecosystem,
            settings=settings, vec_store=vec_store, embed=embed,
        ))

    def read_page(
        self,
        url: str,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
    ) -> dict[str, Any] | None:
        """按 URL 取该页全部 documents，按 sort_order 重组全文。"""
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
        for content, raw, order in ((r[1], r[2], None) for r in rows):
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
        """按序号取前后窗口，用于命中后扩展上下文。"""
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
        """按源分组取整节，用于由子块回溯父节。"""
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

    def list_libraries(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT l.id, l.ecosystem, l.name, l.description, l.docs_url, l.resolved_from, "
            "(SELECT count(*) FROM versions v WHERE v.library_id = l.id) AS versions "
            "FROM libraries l ORDER BY l.ecosystem, l.name"
        ).fetchall()
        return [
            {
                "id": r[0],
                "ecosystem": r[1],
                "name": r[2],
                "description": r[3],
                "docs_url": r[4],
                "resolved_from": r[5],
                "versions": r[6],
            }
            for r in rows
        ]

    def list_versions(
        self, library: str, ecosystem: str | None = None
    ) -> list[dict[str, Any]]:
        if ecosystem is None:
            rows = self._conn.execute(
                "SELECT v.id, v.version, v.registry_confirmed, v.created_at, l.ecosystem, "
                "(SELECT count(*) FROM pages p WHERE p.version_id = v.id) AS pages "
                "FROM versions v JOIN libraries l ON l.id = v.library_id "
                "WHERE l.name = ? ORDER BY v.created_at",
                (library,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT v.id, v.version, v.registry_confirmed, v.created_at, l.ecosystem, "
                "(SELECT count(*) FROM pages p WHERE p.version_id = v.id) AS pages "
                "FROM versions v JOIN libraries l ON l.id = v.library_id "
                "WHERE l.name = ? AND l.ecosystem = ? ORDER BY v.created_at",
                (library, ecosystem),
            ).fetchall()
        return [
            {
                "id": r[0],
                "version": r[1],
                "registry_confirmed": bool(r[2]),
                "created_at": r[3],
                "ecosystem": r[4],
                "pages": r[5],
            }
            for r in rows
        ]

    def _mining_version_ids(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
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
        rows = self._conn.execute(
            "SELECT v.id FROM versions v JOIN libraries l ON l.id = v.library_id "
            f"{where}",
            params,
        ).fetchall()
        return [int(r[0]) for r in rows]

    def api_usage_counts(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        include_ambiguous: bool = False,
    ) -> list[dict[str, Any]]:
        vids = self._mining_version_ids(library, version, ecosystem)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        conf = "" if include_ambiguous else "AND confidence != 'AMBIGUOUS'"
        sql = (
            "SELECT callee_name, count(*), "
            "count(DISTINCT caller_file || char(0) || COALESCE(caller, '')) "
            "FROM call_instances "
            f"WHERE version_id IN ({placeholders}) {conf} "
            "GROUP BY callee_name ORDER BY count(*) DESC, callee_name"
        )
        params: list[Any] = list(vids)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [
            {"api": r[0], "calls": r[1], "scopes": r[2]}
            for r in self._conn.execute(sql, params).fetchall()
        ]

    def arg_shapes(
        self,
        api: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 10,
        include_ambiguous: bool = False,
    ) -> list[dict[str, Any]]:
        vids = self._mining_version_ids(library, version, ecosystem)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        conf = "" if include_ambiguous else "AND confidence != 'AMBIGUOUS'"
        rows = self._conn.execute(
            "SELECT args, count(*) FROM call_instances "
            f"WHERE version_id IN ({placeholders}) AND callee_name = ? {conf} "
            "GROUP BY args ORDER BY count(*) DESC LIMIT ?",
            (*vids, api, limit),
        ).fetchall()
        out = []
        for raw, count in rows:
            try:
                parsed = json.loads(raw) if raw else []
            except ValueError:
                parsed = [raw]
            out.append({"args": parsed, "count": count})
        return out

    def related_apis(
        self,
        api: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 10,
        include_ambiguous: bool = False,
    ) -> list[dict[str, Any]]:
        vids = self._mining_version_ids(library, version, ecosystem)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        conf = "" if include_ambiguous else "AND c.confidence != 'AMBIGUOUS'"
        tconf = "" if include_ambiguous else "AND confidence != 'AMBIGUOUS'"
        rows = self._conn.execute(
            "WITH target(file, caller_key) AS ("
            " SELECT DISTINCT caller_file, COALESCE(caller, '')"
            " FROM call_instances"
            f" WHERE version_id IN ({placeholders}) AND callee_name = ? {tconf})"
            " SELECT c.callee_name,"
            " count(DISTINCT c.caller_file || char(0) || COALESCE(c.caller, '')),"
            " count(*) FROM call_instances c"
            " JOIN target t ON c.caller_file = t.file"
            " AND COALESCE(c.caller, '') = t.caller_key"
            f" WHERE c.version_id IN ({placeholders}) AND c.callee_name != ? {conf}"
            " GROUP BY c.callee_name"
            " ORDER BY 2 DESC, 3 DESC, c.callee_name LIMIT ?",
            (*vids, api, *vids, api, limit),
        ).fetchall()
        return [
            {"api": r[0], "scopes": r[1], "calls": r[2]} for r in rows
        ]

    def list_patterns(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        containing: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        vids = self._mining_version_ids(library, version, ecosystem)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        rows = self._conn.execute(
            "SELECT pattern, support FROM seq_patterns"
            f" WHERE version_id IN ({placeholders})"
            " ORDER BY support DESC LIMIT ?",
            (*vids, limit),
        ).fetchall()
        out = []
        for raw, support in rows:
            try:
                names = json.loads(raw)
            except ValueError:
                continue
            if containing and not all(n in names for n in containing):
                continue
            out.append({"pattern": names, "support": support})
        return out

    def pattern_evidence(
        self,
        pattern: list[str],
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 5,
        include_ambiguous: bool = False,
    ) -> list[dict[str, Any]]:
        if not pattern:
            return []
        vids = self._mining_version_ids(library, version, ecosystem)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        conf = "" if include_ambiguous else "AND ci.confidence != 'AMBIGUOUS'"
        rows = self._conn.execute(
            "SELECT ci.version_id, l.name, v.version, ci.caller_file, ci.caller,"
            " ci.callee_name, ci.args, ci.line_start, ci.line_end, ci.confidence,"
            " ci.call_order, ci.start_byte, ci.id"
            " FROM call_instances ci"
            " JOIN versions v ON v.id = ci.version_id"
            " JOIN libraries l ON l.id = v.library_id"
            f" WHERE ci.version_id IN ({placeholders}) {conf}"
            " ORDER BY ci.version_id, ci.caller_file, ci.line_start,"
            " ci.start_byte, ci.call_order, ci.id",
            tuple(vids),
        ).fetchall()
        groups: dict[tuple, dict[str, Any]] = {}
        order: list[tuple] = []
        for vid, lib, ver, path, caller, callee, args, ls, le, cf, _o, _s, _i in rows:
            key = (vid, path, caller or "")
            slot = groups.get(key)
            if slot is None:
                slot = {
                    "library": lib,
                    "version": ver,
                    "caller_file": path,
                    "caller": caller,
                    "calls": [],
                }
                groups[key] = slot
                order.append(key)
            try:
                parsed = json.loads(args) if args else []
            except ValueError:
                parsed = [args]
            slot["calls"].append({
                "callee": callee,
                "args": parsed,
                "line_start": ls,
                "line_end": le,
                "confidence": cf,
            })
        out = []
        for key in order:
            names = [c["callee"] for c in groups[key]["calls"]]
            if _is_subsequence(pattern, names):
                out.append(groups[key])
                if len(out) >= limit:
                    break
        return out

    def feature_tree(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        prefix: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        vids = self._mining_version_ids(library, version, ecosystem)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        params: list[Any] = list(vids)
        filt = ""
        if prefix:
            filt = " AND (path = ? OR path LIKE ? ESCAPE '\\')"
            params.extend([prefix, prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ".%"])
        params.append(limit)
        rows = self._conn.execute(
            "SELECT path, parent, depth, kind, calls, scopes FROM feature_nodes"
            f" WHERE version_id IN ({placeholders}){filt}"
            " ORDER BY calls DESC, path LIMIT ?",
            params,
        ).fetchall()
        return [
            {"path": r[0], "parent": r[1], "depth": r[2], "kind": r[3], "calls": r[4], "scopes": r[5]}
            for r in rows
        ]

    def api_clusters(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        containing: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        vids = self._mining_version_ids(library, version, ecosystem)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        rows = self._conn.execute(
            "SELECT cluster_id, member, size, representative FROM api_clusters"
            f" WHERE version_id IN ({placeholders})"
            " ORDER BY cluster_id, member",
            tuple(vids),
        ).fetchall()
        grouped: dict[int, dict[str, Any]] = {}
        order: list[int] = []
        for cid, member, size, rep in rows:
            slot = grouped.get(int(cid))
            if slot is None:
                slot = {"cluster_id": int(cid), "members": [], "size": int(size), "representative": rep}
                grouped[int(cid)] = slot
                order.append(int(cid))
            slot["members"].append(member)
        out = [grouped[cid] for cid in order]
        if containing:
            out = [g for g in out if all(n in g["members"] for n in containing)]
        return out[:limit]

    def ensure_library_version(
        self,
        library: str,
        version: str,
        ecosystem: str = "custom",
        *,
        homepage: str | None = None,
        docs_url: str | None = None,
        repo_url: str | None = None,
        resolved_from: str | None = None,
        registry_confirmed: bool = True,
    ) -> tuple[int, int]:
        """确保库/版本行存在，返回 (library_id, version_id)。"""
        self._conn.execute(
            "INSERT INTO libraries(ecosystem, name) VALUES(?,?) "
            "ON CONFLICT(ecosystem, name) DO NOTHING",
            (ecosystem, library),
        )
        row = self._conn.execute(
            "SELECT id FROM libraries WHERE ecosystem=? AND name=?", (ecosystem, library)
        ).fetchone()
        library_id = int(row[0])
        if homepage is not None or docs_url is not None or repo_url is not None or resolved_from is not None:
            self._conn.execute(
                "UPDATE libraries SET homepage=COALESCE(?, homepage), docs_url=COALESCE(?, docs_url), "
                "repo_url=COALESCE(?, repo_url), resolved_from=COALESCE(?, resolved_from), "
                "resolved_at=datetime('now') WHERE id=?",
                (homepage, docs_url, repo_url, resolved_from, library_id),
            )
        self._conn.execute(
            "INSERT INTO versions(library_id, version, registry_confirmed) VALUES(?,?,?) "
            "ON CONFLICT(library_id, version) DO NOTHING",
            (library_id, version, 1 if registry_confirmed else 0),
        )
        self._conn.execute(
            "UPDATE versions SET registry_confirmed=? WHERE library_id=? AND version=?",
            (1 if registry_confirmed else 0, library_id, version),
        )
        row = self._conn.execute(
            "SELECT id FROM versions WHERE library_id=? AND version=?", (library_id, version)
        ).fetchone()
        self._conn.commit()
        return library_id, int(row[0])
