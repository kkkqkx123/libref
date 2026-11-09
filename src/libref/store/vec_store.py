"""Vector index wrapper, a derived index over the SQLite source of truth.

Vector point ids map one-to-one to document row ids; the payload stores only filter
keys and body text is fetched back from SQLite. Callers are the orchestration layer;
the API layer never touches this directly.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from typing import Any

from qdrant_client import QdrantClient, models


def collection_for(base: str, db_path: str | os.PathLike[str]) -> str:
    """Derive the collection name from the database file identity; different db files are naturally isolated, in-memory dbs keep the base name."""
    raw = os.fspath(db_path)
    if raw == ":memory:":
        return base
    digest = hashlib.sha1(os.path.abspath(raw).encode("utf-8")).hexdigest()[:12]
    return f"{base}_{digest}"


class QdrantVecStore:
    """Single collection with payload-version filtering; distance is cosine."""

    def __init__(
        self,
        url: str,
        collection: str,
        *,
        timeout: float = 5.0,
        client: Any | None = None,
    ) -> None:
        self.collection = collection
        self.client = (
            client if client is not None else QdrantClient(url=url, timeout=timeout)
        )

    # ── Collection governance ──

    def ensure_collection(self, conn: sqlite3.Connection, model: str, dim: int) -> None:
        """Create or verify the collection and register model metadata.

        When the collection does not exist, create it with the given dimension plus
        filter indexes. If the model name, dimension or records mismatch, or the
        collection shape is unrecognizable, delete and recreate it and update the
        records. When records match, this is an idempotent no-op.
        """
        old_model, old_dim = self._read_meta(conn)
        if self.client.collection_exists(self.collection):
            cur_dim = self._collection_dim()
            if cur_dim != dim or old_dim != dim or old_model != model:
                self.client.delete_collection(self.collection)
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(
                    size=dim, distance=models.Distance.COSINE
                ),
            )
            self._create_payload_indexes()
        self._write_meta(conn, model, dim)

    def _collection_dim(self) -> int | None:
        info = self.client.get_collection(self.collection)
        vectors = info.config.params.vectors
        if isinstance(vectors, models.VectorParams):
            return vectors.size
        return None

    def _create_payload_indexes(self) -> None:
        # Build indexes before loading data; version_id is the filter key, and the
        # integer tenant optimization keeps equal values linked
        self.client.create_payload_index(
            collection_name=self.collection,
            field_name="version_id",
            field_schema=models.IntegerIndexParams(
                type=models.IntegerIndexType.INTEGER, is_principal=True
            ),
        )
        self.client.create_payload_index(
            collection_name=self.collection,
            field_name="library_id",
            field_schema=models.PayloadSchemaType.INTEGER,
        )

    @staticmethod
    def _read_meta(conn: sqlite3.Connection) -> tuple[str | None, int | None]:
        rows = dict(
            conn.execute(
                "SELECT key, value FROM schema_meta WHERE key IN ('vec_model','vec_dim')"
            )
        )
        model = rows.get("vec_model")
        dim = int(rows["vec_dim"]) if "vec_dim" in rows else None
        return model, dim

    @staticmethod
    def _write_meta(conn: sqlite3.Connection, model: str, dim: int) -> None:
        conn.executemany(
            "INSERT INTO schema_meta(key, value) VALUES(?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (("vec_model", model), ("vec_dim", str(dim))),
        )
        conn.commit()

    # ── Write ──

    def upsert_batch(
        self,
        points: list[tuple[int, int, int, list[float]]],
        *,
        wait: bool = True,
    ) -> None:
        """Batch-write document, library and version ids with vectors, idempotent overwrite."""
        if not points:
            return
        self.client.upsert(
            collection_name=self.collection,
            points=[
                models.PointStruct(
                    id=doc_id,
                    vector=embedding,
                    payload={"library_id": library_id, "version_id": version_id},
                )
                for doc_id, library_id, version_id, embedding in points
            ],
            wait=wait,
        )

    def delete_docs(self, doc_ids: list[int]) -> None:
        """Delete vector points for the given documents, cleaning up old points on single-page re-chunking."""
        if not doc_ids:
            return
        self.client.delete(
            collection_name=self.collection,
            points_selector=models.PointIdsList(points=doc_ids),
        )

    def delete_version(self, version_id: int) -> None:
        """Delete all vector points of a version, on re-crawl of that version."""
        self.client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(
                filter=self._version_filter(version_id)
            ),
        )

    # ── Search and reconciliation ──

    def search(
        self, query_vec: list[float], *, k: int, version_id: int
    ) -> list[tuple[int, float]]:
        """Nearest-neighbor search filtered by version; returns document ids and cosine similarity in descending similarity order."""
        res = self.client.query_points(
            collection_name=self.collection,
            query=query_vec,
            query_filter=self._version_filter(version_id),
            limit=k,
            with_payload=False,
        )
        return [(p.id, p.score) for p in res.points]

    def count(self, version_id: int) -> int:
        """Number of vector points in a version, for reconciliation against the documents table count."""
        return self.client.count(
            collection_name=self.collection,
            count_filter=self._version_filter(version_id),
            exact=True,
        ).count

    @staticmethod
    def _version_filter(version_id: int) -> models.Filter:
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="version_id", match=models.MatchValue(value=version_id)
                )
            ]
        )
