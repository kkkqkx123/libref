"""检索服务：全文检索加页面读取加库版本列举。"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

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
    ) -> dict[str, Any] | None:
        """按 URL 取该页全部 documents，按 sort_order 重组全文。"""
        filters = ["p.url = ?"]
        params: list[Any] = [url]
        if library is not None:
            filters.append("l.name = ?")
            params.append(library)
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
            "SELECT l.id, l.name, l.description, "
            "(SELECT count(*) FROM versions v WHERE v.library_id = l.id) AS versions "
            "FROM libraries l ORDER BY l.name"
        ).fetchall()
        return [
            {"id": r[0], "name": r[1], "description": r[2], "versions": r[3]} for r in rows
        ]

    def list_versions(self, library: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT v.id, v.version, v.created_at, "
            "(SELECT count(*) FROM pages p WHERE p.version_id = v.id) AS pages "
            "FROM versions v JOIN libraries l ON l.id = v.library_id "
            "WHERE l.name = ? ORDER BY v.created_at",
            (library,),
        ).fetchall()
        return [{"id": r[0], "version": r[1], "created_at": r[2], "pages": r[3]} for r in rows]

    def ensure_library_version(self, library: str, version: str) -> tuple[int, int]:
        """确保库/版本行存在，返回 (library_id, version_id)。"""
        self._conn.execute(
            "INSERT INTO libraries(name) VALUES(?) ON CONFLICT(name) DO NOTHING", (library,)
        )
        row = self._conn.execute("SELECT id FROM libraries WHERE name=?", (library,)).fetchone()
        library_id = int(row[0])
        self._conn.execute(
            "INSERT INTO versions(library_id, version) VALUES(?,?) "
            "ON CONFLICT(library_id, version) DO NOTHING",
            (library_id, version),
        )
        row = self._conn.execute(
            "SELECT id FROM versions WHERE library_id=? AND version=?", (library_id, version)
        ).fetchone()
        self._conn.commit()
        return library_id, int(row[0])
