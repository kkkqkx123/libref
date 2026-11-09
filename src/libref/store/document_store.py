"""页面与文档块的读写入口，增量结局共六种。

全文索引由 SQLite 触发器随文档表自动同步，本层只写页面与文档两张表，
不做索引双写。每次存储同时返回同步增量，供调用方同步派生索引。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
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


@dataclass
class StoreResult:
    outcome: str
    removed_ids: list[int] = field(default_factory=list)
    inserted_ids: list[int] = field(default_factory=list)
    reused_ids: list[int] = field(default_factory=list)


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
        """列出该版本下已有页面的全部地址，供调用方做缺席差集。"""
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

    def get_window(self, page_id: int, sort_order: int, radius: int = 1, *, chunk_path: str | None = None) -> list[dict[str, Any]]:
        """按排序序号取前后窗口，供检索命中扩展上下文。"""
        if radius < 0:
            return []
        if chunk_path is None:
            rows = self._conn.execute(
                "SELECT id, url, content, metadata, sort_order FROM documents "
                "WHERE page_id=? AND sort_order BETWEEN ? AND ? ORDER BY sort_order",
                (page_id, sort_order - radius, sort_order + radius),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, url, content, metadata, sort_order FROM documents "
                "WHERE page_id=? AND sort_order BETWEEN ? AND ? "
                "AND json_extract(metadata, '$.chunk_path')=? ORDER BY sort_order",
                (page_id, sort_order - radius, sort_order + radius, chunk_path),
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

    def get_documents(self, doc_ids: list[int]) -> list[dict[str, Any]]:
        """按标识批量取回文档，供派生索引编码取材与孤儿过滤。"""
        if not doc_ids:
            return []
        marks = ",".join("?" for _ in doc_ids)
        rows = self._conn.execute(
            "SELECT id, library_id, version_id, url, content, metadata, sort_order "
            f"FROM documents WHERE id IN ({marks}) ORDER BY id",
            doc_ids,
        ).fetchall()
        found = [
            {
                "id": r[0],
                "library_id": r[1],
                "version_id": r[2],
                "url": r[3],
                "content": r[4],
                "metadata": json.loads(r[5]) if r[5] else {},
                "sort_order": r[6],
            }
            for r in rows
        ]
        by_id = {d["id"]: d for d in found}
        return [by_id[i] for i in doc_ids if i in by_id]

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
    ) -> StoreResult:
        """按结局写库并返回结局与文档增量，单事务提交。

        存入新内容时先记下被替换的旧文档标识，写入后返回新文档标识。
        删除整页时返回被级联删掉的文档标识。内容不变与跳过失败时增量为空。
        """
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome: {outcome}")
        try:
            if outcome == "absent":
                removed = self._delete_page(version_id, url)
                self._conn.commit()
                return StoreResult(outcome, removed_ids=removed)
            if outcome in ("skipped", "failed"):
                self._conn.commit()
                return StoreResult(outcome)
            if outcome == "empty":
                self._upsert_page(version_id, url, etag, last_modified, raw_content)
                page = self.get_page(version_id, url)
                assert page is not None
                removed = self._delete_documents(page.page_id)
                self._conn.commit()
                return StoreResult(outcome, removed_ids=removed)
            if outcome == "unchanged":
                page = self.get_page(version_id, url)
                if page is not None:
                    self._conn.execute(
                        "UPDATE pages SET fetched_at=datetime('now') WHERE id=?",
                        (page.page_id,),
                    )
                self._conn.commit()
                return StoreResult(outcome)
            self._upsert_page(version_id, url, etag, last_modified, raw_content)
            page = self.get_page(version_id, url)
            assert page is not None
            existing = self.list_page_documents(page.page_id)
            by_hash: dict[str, list[dict[str, Any]]] = {}
            for row in existing:
                try:
                    meta = row.get("metadata") or {}
                except AttributeError:
                    meta = {}
                digest = str(meta.get("content_hash") or "") or content_hash(row.get("content"))
                by_hash.setdefault(digest, []).append(row)
            inserted: list[int] = []
            reused: list[int] = []
            used_old: set[int] = set()
            pending: list[tuple[Chunk, str]] = []
            for chunk in chunks or []:
                digest = getattr(chunk, "content_hash", "") or content_hash(chunk.content)
                candidates = by_hash.get(digest, [])
                hit = next((r for r in candidates if r["id"] not in used_old), None)
                if hit is not None:
                    used_old.add(int(hit["id"]))
                    reused.append(int(hit["id"]))
                    pending.append((chunk, str(hit["id"])))
                else:
                    pending.append((chunk, ""))
            leftover = [r["id"] for r in existing if r["id"] not in used_old]
            # 先删无复用的旧行，复用行改为更新排序与元数据，避免整页抖动。
            if leftover:
                marks = ",".join("?" for _ in leftover)
                self._conn.execute(
                    f"DELETE FROM documents WHERE id IN ({marks})", leftover
                )
            for chunk, reuse_id in pending:
                metadata = json.dumps(
                    {
                        "title": chunk.title,
                        "path": chunk.path,
                        "chunk_path": getattr(chunk, "chunk_path", "embedding"),
                        "source_group_id": getattr(chunk, "source_group_id", ""),
                        "chunk_id": getattr(chunk, "chunk_id", ""),
                        "span_kind": getattr(chunk, "span_kind", "document_range"),
                        "merged_from": list(getattr(chunk, "merged_from", []) or []),
                        "overlap_from": getattr(chunk, "overlap_from", None),
                        "content_hash": getattr(chunk, "content_hash", "") or content_hash(chunk.content),
                        "keywords": list(getattr(chunk, "keywords", []) or []),
                        "content_type": getattr(chunk, "content_type", "text/markdown"),
                        "start_char": getattr(chunk, "start_char", -1),
                        "end_char": getattr(chunk, "end_char", -1),
                        "prev_chunk_id": getattr(chunk, "prev_chunk_id", None),
                        "next_chunk_id": getattr(chunk, "next_chunk_id", None),
                    },
                    ensure_ascii=False,
                )
                if reuse_id:
                    self._conn.execute(
                        "UPDATE documents SET url=?, content=?, metadata=?, sort_order=? WHERE id=?",
                        (chunk.url, chunk.content, metadata, chunk.sort_order, int(reuse_id)),
                    )
                else:
                    cur = self._conn.execute(
                        "INSERT INTO documents(library_id, version_id, page_id, url, content, metadata, sort_order) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (library_id, version_id, page.page_id, chunk.url,
                         chunk.content, metadata, chunk.sort_order),
                    )
                    inserted.append(int(cur.lastrowid))
            self._conn.commit()
            return StoreResult(outcome, removed_ids=leftover, inserted_ids=inserted, reused_ids=reused)
        except BaseException:
            self._conn.rollback()
            raise

    def ensure_changed(self, version_id: int, url: str, raw_content: str | None) -> str:
        """按内容指纹判定存入还是不变，服务端未给 304 时的二次短路。"""
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

    def _delete_page(self, version_id: int, url: str) -> list[int]:
        """删除整页，返回被外键级联删掉的文档标识。"""
        page = self.get_page(version_id, url)
        if page is None:
            return []
        rows = self._conn.execute(
            "SELECT id FROM documents WHERE page_id=?", (page.page_id,)
        ).fetchall()
        removed = [r[0] for r in rows]
        self._conn.execute("DELETE FROM pages WHERE version_id=? AND url=?", (version_id, url))
        return removed

    def _delete_documents(self, page_id: int) -> list[int]:
        """清空一页的文档块，返回删掉的标识。"""
        rows = self._conn.execute(
            "SELECT id FROM documents WHERE page_id=?", (page_id,)
        ).fetchall()
        removed = [r[0] for r in rows]
        self._conn.execute("DELETE FROM documents WHERE page_id=?", (page_id,))
        return removed
