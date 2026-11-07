"""应用配置（pydantic-settings）。

定制点：P0 仅放已确定字段——db 路径、P1 抓取参数占位、P3 embedding 模型名/维度
（tech-stack §1：记录模型名+维度，变更时整体失效向量）。
环境变量前缀 `LIBREF_`，如 `LIBREF_DB_PATH`。
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class LibrefSettings(BaseSettings):
    """全局配置；出口层与 services 层共用，db 层不依赖本模块。"""

    model_config = SettingsConfigDict(env_prefix="LIBREF_")

    db_path: Path = Path("data/libref.db")
    # P1 抓取参数占位（crawler/fetcher 使用）
    fetch_concurrency: int = 4
    request_timeout: float = 30.0
    # 向量索引（Qdrant，docs/plan/qdrant-migration.md）
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "libref_chunks"
    qdrant_timeout: float = 5.0
    # P3 embedding 参数占位（store/embeddings 使用）
    embedding_model: str = "BAAI/bge-small-zh"
    embedding_dim: int = 512
