"""Qdrant 向量索引封装。

蓝本：docs-mcp-server 的 ensureVectorTable 维度校验/重建语义，迁移至 Qdrant
（方案见 docs/plan/qdrant-migration.md）。SQLite 仍是唯一真源，本模块只管理
可重建的派生向量索引；调用方为 services 编排层，出口层不直接触达。
"""

from __future__ import annotations

import sqlite3

from qdrant_client import QdrantClient, models

# Point id = documents.id；payload 只存过滤键，正文回 SQLite 取
_FILTER_KEYS = ("library_id", "version_id")


class QdrantVecStore:
    """单集合 + payload 版本过滤（官方 multitenancy 建议），distance=COSINE。"""

    def __init__(
        self,
        url: str,
        collection: str,
        *,
        timeout: float = 5.0,
    ) -> None:
        self.collection = collection
        self.client = QdrantClient(url=url, timeout=timeout)

    # ── 集合治理 ──

    def ensure_collection(self, conn: sqlite3.Connection, model: str, dim: int) -> None:
        """建/校验集合并登记模型元数据（蓝本 ensureVectorTable 语义）。

        - 集合不存在：按 dim 建集合 + payload 过滤索引；
        - model 或 dim 与 schema_meta 记录不符：删除重建（整体失效向量）；
        - 一致：幂等 no-op。
        """
        old_model, old_dim = self._read_meta(conn)
        if self.client.collection_exists(self.collection):
            cur_dim = self._collection_dim()
            if cur_dim is not None and (cur_dim != dim or old_model != model):
                # 模型/维度变更 → 删除重建（整体失效向量，由 embeddings 层重灌）
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
        return None  # named vectors 等非预期形态，交由调用方重建

    def _create_payload_indexes(self) -> None:
        # 灌数前建索引（官方建议）；version_id 是租户键
        # integer 类型的租户优化开关是 is_principal（keyword/uuid 才叫 is_tenant）：
        # HNSW 保证同租户值互链，提升版本过滤下推效率（已在本环境验证）
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
        """批量写入：(doc_id, library_id, version_id, embedding)。幂等覆盖。"""
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
        """删除指定文档的向量点（单文档重分块场景）。"""
        if not doc_ids:
            return
        self.client.delete(
            collection_name=self.collection,
            points_selector=models.PointIdsList(points=doc_ids),
        )

    def delete_version(self, version_id: int) -> None:
        """删除整版本向量点（版本重抓场景）。"""
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
        """KNN 检索：按 version_id 过滤，返回 [(doc_id, score)]，COSINE similarity 降序
        （score 越大越相似——注意与 sqlite-vec 时代 distance 语义相反）。
        query 向量由 P3 store/embeddings 层编码后传入。
        """
        res = self.client.query_points(
            collection_name=self.collection,
            query=query_vec,
            query_filter=self._version_filter(version_id),
            limit=k,
            with_payload=False,
        )
        return [(p.id, p.score) for p in res.points]

    def count(self, version_id: int) -> int:
        """版本内向量点数，供与 SQL documents 计数对账（docs/plan/qdrant-migration.md §4）。"""
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
