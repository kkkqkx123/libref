"""Read/write entry point for pages and document chunks, with six sync outcomes in total.

The full-text index is kept in sync automatically by SQLite triggers on the documents
table; this layer writes only the pages and documents tables and never double-writes
the index. Each store call also returns the sync delta so callers can update derived
indexes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

OUTCOMES = ("stored", "unchanged", "absent", "empty", "skipped", "failed")


def content_hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass
class DocumentInput:
    """Plain document block for storage, no chunking types cross the boundary."""

    url: str
    path: str
    title: str
    content: str
    sort_order: int
    chunk_path: str = "embedding"
    source_group_id: str = ""
    chunk_id: str = ""
    span_kind: str = "document_range"
    merged_from: list[str] = field(default_factory=list)
    overlap_from: str | None = None
    overlap_text: str = ""
    chunk_index: int = 0
    total_chunks: int = 1
    content_hash: str = ""
    keywords: list[str] = field(default_factory=list)
    content_type: str = "text/markdown"
    start_char: int = -1
    end_char: int = -1
    prev_chunk_id: str | None = None
    next_chunk_id: str | None = None
    entities: list[str] = field(default_factory=list)


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

    # ── Read ──

    def get_page(self, version_id: int, url: str) -> PageRecord | None:
        row = self._conn.execute(
            "SELECT id, url, etag, last_modified, content_hash "
            "FROM pages WHERE version_id=? AND url=?",
            (version_id, url),
        ).fetchone()
        if row is None:
            return None
        return PageRecord(
            page_id=row[0],
            url=row[1],
            etag=row[2],
            last_modified=row[3],
            content_hash=row[4],
        )

    def list_page_urls(self, version_id: int) -> list[str]:
        """List all URLs of existing pages under this version, for the caller's absent-set difference."""
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

    def get_window(
        self,
        page_id: int,
        sort_order: int,
        radius: int = 1,
        *,
        chunk_path: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch a before/after window by sort order, for expanding retrieval hit context."""
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
        """Batch-fetch documents by id, for derived-index encoding and orphan filtering."""
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

    # ── Write: the single entry point ──

    def store_page(
        self,
        version_id: int,
        library_id: int,
        url: str,
        outcome: str,
        *,
        chunks: list[DocumentInput] | None = None,
        raw_content: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> StoreResult:
        """Write per outcome and return the outcome plus document delta, committed in a single transaction.

        When storing new content, first record the ids of the old documents being replaced,
        then return the new document ids after writing. Deleting a whole page returns the
        document ids removed by cascade. Unchanged, skipped and failed outcomes yield an
        empty delta.
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
                stored = str(meta.get("content_hash") or "")
                if stored:
                    digest = stored
                else:
                    digest = content_hash(
                        f"{meta.get('title', '')}\0{meta.get('path', '')}\0{row.get('content')}"
                    )
                by_hash.setdefault(digest, []).append(row)
            inserted: list[int] = []
            reused: list[int] = []
            used_old: set[int] = set()
            pending: list[tuple[DocumentInput, str]] = []
            for chunk in chunks or []:
                digest = getattr(chunk, "content_hash", "") or content_hash(
                    f"{chunk.title}\0{chunk.path}\0{chunk.content}"
                )
                candidates = by_hash.get(digest, [])
                hit = next((r for r in candidates if r["id"] not in used_old), None)
                if hit is not None:
                    used_old.add(int(hit["id"]))
                    reused.append(int(hit["id"]))
                    pending.append((chunk, str(hit["id"])))
                else:
                    pending.append((chunk, ""))
            leftover = [r["id"] for r in existing if r["id"] not in used_old]
            # Delete old rows with no reuse first; reused rows become updates of sort order
            # and metadata, avoiding whole-page churn.
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
                        "overlap_text": str(getattr(chunk, "overlap_text", "") or ""),
                        "chunk_index": int(getattr(chunk, "chunk_index", 0) or 0),
                        "total_chunks": max(
                            1, int(getattr(chunk, "total_chunks", 1) or 1)
                        ),
                        "content_hash": getattr(chunk, "content_hash", "")
                        or content_hash(chunk.content),
                        "keywords": list(getattr(chunk, "keywords", []) or []),
                        "content_type": getattr(chunk, "content_type", "text/markdown"),
                        "start_char": getattr(chunk, "start_char", -1),
                        "end_char": getattr(chunk, "end_char", -1),
                        "prev_chunk_id": getattr(chunk, "prev_chunk_id", None),
                        "next_chunk_id": getattr(chunk, "next_chunk_id", None),
                        "entities": list(getattr(chunk, "entities", []) or []),
                    },
                    ensure_ascii=False,
                )
                if reuse_id:
                    self._conn.execute(
                        "UPDATE documents SET url=?, content=?, metadata=?, sort_order=? WHERE id=?",
                        (
                            chunk.url,
                            chunk.content,
                            metadata,
                            chunk.sort_order,
                            int(reuse_id),
                        ),
                    )
                else:
                    cur = self._conn.execute(
                        "INSERT INTO documents(library_id, version_id, page_id, url, content, metadata, sort_order) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            library_id,
                            version_id,
                            page.page_id,
                            chunk.url,
                            chunk.content,
                            metadata,
                            chunk.sort_order,
                        ),
                    )
                    assert cur.lastrowid is not None
                    inserted.append(int(cur.lastrowid))
            self._conn.commit()
            return StoreResult(
                outcome, removed_ids=leftover, inserted_ids=inserted, reused_ids=reused
            )
        except BaseException:
            self._conn.rollback()
            raise

    def ensure_changed(self, version_id: int, url: str, raw_content: str | None) -> str:
        """Decide stored vs unchanged by content fingerprint; a second short-circuit when the server did not return 304."""
        page = self.get_page(version_id, url)
        if (
            page is not None
            and page.content_hash is not None
            and page.content_hash == content_hash(raw_content)
        ):
            return "unchanged"
        return "stored"

    # ── Internal ──

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
        """Delete a whole page and return the document ids removed by foreign-key cascade."""
        page = self.get_page(version_id, url)
        if page is None:
            return []
        rows = self._conn.execute(
            "SELECT id FROM documents WHERE page_id=?", (page.page_id,)
        ).fetchall()
        removed = [r[0] for r in rows]
        self._conn.execute(
            "DELETE FROM pages WHERE version_id=? AND url=?", (version_id, url)
        )
        return removed

    def _delete_documents(self, page_id: int) -> list[int]:
        """Clear all document chunks of one page and return the removed ids."""
        rows = self._conn.execute(
            "SELECT id FROM documents WHERE page_id=?", (page_id,)
        ).fetchall()
        removed = [r[0] for r in rows]
        self._conn.execute("DELETE FROM documents WHERE page_id=?", (page_id,))
        return removed
