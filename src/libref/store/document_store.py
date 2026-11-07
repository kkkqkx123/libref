"""pages/documents 读写与增量结局六态。

蓝本 docs-mcp-server 增量抓取语义：stored/unchanged/absent/empty/skipped/failed。
FTS 由 db/schema.py 触发器自动同步，本层不做 FTS 双写。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from libref.chunking.splitter import Chunk

OUTCOMES = ("stored", "unchanged", "absent", "empty", "skipped", "failed")


def content_hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass
class PageRecord:
    page_id: int
    url: str
    etag: str | None
    last_modified: str | None
    content_hash: str | None


class DocumentStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── 读 ──

    def get_page(self, version_id: int, url: str) -> PageRecord | None:
        row = self._conn.execute(
            "SELECT id, url, etag, last_modified, content_hash "
            "FROM pages WHERE version_id=? AND url=?",
            (version_id, url),
        ).fetchone()
        if row is None:
            return None
        return PageRecord(page_id=row[0], url=row[1], etag=row[2],
                          last_modified=row[3], content_hash=row[4])

    def list_page_urls(self, version_id: int) -> list[str]:
        """列出该版本下已有 pages 的全部 URL（供 ingest 缺席清理做差集）。"""
        rows = self._conn.execute(
            "SELECT url FROM pages WHERE version_id=? ORDER BY url",
            (version_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def list_page_documents(self, page_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, url, content, metadata, sort_order FROM documents "
            "WHERE page_id=? ORDER BY sort_order",
            (page_id,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "url": r[1],
                "content": r[2],
                "metadata": json.loads(r[3]) if r[3] else {},
                "sort_order": r[4],
            }
            for r in rows
        ]

    # ── 写：唯一入口 ──

    def store_page(
        self,
        version_id: int,
        library_id: int,
        url: str,
        outcome: str,
        *,
        chunks: list[Chunk] | None = None,
        raw_content: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> str:
        """按结局写库，返回结局名。stored 时 chunks 必填；unchanged 时更新 fetched_at。"""
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome: {outcome}")

        if outcome == "absent":
            self._delete_page(version_id, url)
            return outcome

        if outcome in ("skipped", "failed"):
            self._conn.commit()
            return outcome

        if outcome == "empty":
            # empty 保留 page 行（记录 etag 状态）但确保无 documents
            self._upsert_page(version_id, url, etag, last_modified, raw_content)
            page = self.get_page(version_id, url)
            assert page is not None
            self._delete_documents(page.page_id)
            return outcome

        if outcome == "unchanged":
            page = self.get_page(version_id, url)
            if page is not None:
                self._conn.execute(
                    "UPDATE pages SET fetched_at=datetime('now') WHERE id=?", (page.page_id,)
                )
                self._conn.commit()
            return outcome

        # stored：新页面或内容变化
        self._upsert_page(version_id, url, etag, last_modified, raw_content)
        page = self.get_page(version_id, url)
        assert page is not None
        self._delete_documents(page.page_id)
        for chunk in chunks or []:
            metadata = json.dumps({"title": chunk.title, "path": chunk.path}, ensure_ascii=False)
            self._conn.execute(
                "INSERT INTO documents(library_id, version_id, page_id, url, content, metadata, sort_order) "
                "VALUES(?,?,?,?,?,?,?)",
                (library_id, version_id, page.page_id, chunk.url, chunk.content, metadata, chunk.sort_order),
            )
        self._conn.commit()
        return outcome

    def ensure_changed(self, version_id: int, url: str, raw_content: str | None) -> str:
        """按内容 hash 判定 stored / unchanged（304 之外的二次短路）。"""
        page = self.get_page(version_id, url)
        if (
            page is not None
            and page.content_hash is not None
            and page.content_hash == content_hash(raw_content)
        ):
            return "unchanged"
        return "stored"

    # ── 内部 ──

    def _upsert_page(
        self,
        version_id: int,
        url: str,
        etag: str | None,
        last_modified: str | None,
        raw_content: str | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO pages(version_id, url, etag, last_modified, fetched_at, content_hash) "
            "VALUES(?,?,?,?,datetime('now'),?) "
            "ON CONFLICT(version_id, url) DO UPDATE SET "
            "etag=excluded.etag, last_modified=excluded.last_modified, "
            "fetched_at=datetime('now'), content_hash=excluded.content_hash",
            (version_id, url, etag, last_modified, content_hash(raw_content)),
        )
        self._conn.commit()

    def _delete_page(self, version_id: int, url: str) -> None:
        self._conn.execute("DELETE FROM pages WHERE version_id=? AND url=?", (version_id, url))
        self._conn.commit()

    def _delete_documents(self, page_id: int) -> None:
        self._conn.execute("DELETE FROM documents WHERE page_id=?", (page_id,))
        self._conn.commit()
