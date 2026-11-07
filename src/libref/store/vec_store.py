"""向量索引封装，SQLite 真源的派生索引。

向量点标识与文档行标识一一对应，载荷只存过滤键，正文回 SQLite 取。
调用方为编排层，出口层不直接触达。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from typing import Any

from qdrant_client import QdrantClient, models


def collection_for(base: str, db_path: str | os.PathLike[str]) -> str:
    """按库文件身份派生集合名，不同库文件天然隔离，内存库保持原名。"""
    raw = os.fspath(db_path)
    if raw == ":memory:":
        return base
    digest = hashlib.sha1(os.path.abspath(raw).encode("utf-8")).hexdigest()[:12]
    return f"{base}_{digest}"


def build_embed_text(title: str | None, path: str | None, content: str | None) -> str:
    """组装编码文本，标题路径在前、正文在后，写入与查询共用同一构造。"""
    head = " / ".join(p for p in (title, path) if p)
    body = content or ""
    return f"{head}\n{body}" if head else body


class QdrantVecStore:
    """单集合加载荷版本过滤，距离为余弦。"""

    def __init__(
        self,
        url: str,
        collection: str,
        *,
        timeout: float = 5.0,
        client: Any | None = None,
    ) -> None:
        self.collection = collection
        self.client = client if client is not None else QdrantClient(url=url, timeout=timeout)

    # ── 集合治理 ──

    def ensure_collection(self, conn: sqlite3.Connection, model: str, dim: int) -> None:
        """建或校验集合并登记模型元数据。

        集合不存在时按维度建集合加过滤索引。模型名、维度与记录不符，
        或集合形态无法辨认时，删除重建并更新记录。记录一致时幂等空操作。
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
        # 灌数前建索引；版本标识是过滤键，整数租户优化保证同值互链
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

    # ── 写入 ──

    def upsert_batch(
        self,
        points: list[tuple[int, int, int, list[float]]],
        *,
        wait: bool = True,
    ) -> None:
        """批量写入文档标识、库标识、版本标识与向量，幂等覆盖。"""
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
        """删除指定文档的向量点，单页重分块时清理旧点。"""
        if not doc_ids:
            return
        self.client.delete(
            collection_name=self.collection,
            points_selector=models.PointIdsList(points=doc_ids),
        )

    def delete_version(self, version_id: int) -> None:
        """删除整版本向量点，版本重抓时清理。"""
        self.client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(
                filter=self._version_filter(version_id)
            ),
        )

    # ── 检索与对账 ──

    def search(
        self, query_vec: list[float], *, k: int, version_id: int
    ) -> list[tuple[int, float]]:
        """近邻检索，按版本过滤，返回文档标识与余弦相似度，相似度降序。"""
        res = self.client.query_points(
            collection_name=self.collection,
            query=query_vec,
            query_filter=self._version_filter(version_id),
            limit=k,
            with_payload=False,
        )
        return [(p.id, p.score) for p in res.points]

    def count(self, version_id: int) -> int:
        """版本内向量点数，供与文档表计数对账。"""
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
