"""检索服务：全文检索加页面读取加库版本列举。"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

def _is_subsequence(pattern: list[str], names: list[str]) -> bool:
    pos = 0
    for want in pattern:
        while pos < len(names) and names[pos] != want:
            pos += 1
        if pos == len(names):
            return False
        pos += 1
    return True


_SNIPPET_RADIUS = 60


def _make_snippet(content: str | None, query: str) -> str:
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
    start = max(0, pos - _SNIPPET_RADIUS)
    end = min(len(text), pos + _SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    window = text[start:end]
    # 命中的查询词用 >>> <<< 标记
    for token in sorted(set(re.findall(r"[\w-]+", query)), key=len, reverse=True):
        pattern = re.compile(re.escape(token), re.IGNORECASE)
        window = pattern.sub(lambda m: f">>>{m.group(0)}<<<", window)
    return f"{prefix}{window}{suffix}"


class SearchService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

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
        sql = (
            "SELECT d.id, d.url, bm25(documents_fts, 1.0, 5.0, 1.0, 3.0) AS score, d.content, "
            "json_extract(d.metadata, '$.title') AS title, "
            "json_extract(d.metadata, '$.path') AS path, "
            "l.name AS library, v.version AS version "
            "FROM documents_fts "
            "JOIN documents d ON d.id = documents_fts.rowid "
            "JOIN versions v ON v.id = d.version_id "
            "JOIN libraries l ON l.id = d.library_id "
            f"WHERE documents_fts MATCH ? {where}"
            "ORDER BY score LIMIT ?"
        )
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "id": r[0],
                "url": r[1],
                "score": r[2],
                "snippet": _make_snippet(r[3], query),
                "title": r[4],
                "path": r[5],
                "library": r[6],
                "version": r[7],
            }
            for r in rows
        ]

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
            f"WHERE {' AND '.join(filters)} ORDER BY d.sort_order",
            params,
        ).fetchall()
        if not rows:
            return None
        contents = [r[1] for r in rows if r[1] is not None]
        meta = {}
        if rows[0][2]:
            meta = json.loads(rows[0][2])
        return {
            "url": url,
            "library": rows[0][4],
            "version": rows[0][5],
            "title": meta.get("title"),
            "path": meta.get("path"),
            "content": "\n\n".join(contents),
            "chunks": len(contents),
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
