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

    model_config = SettingsConfigDict(env_prefix="LIBREF_", env_file=".env")

    db_path: Path = Path("data/libref.db")
    # P1 抓取参数（crawler/fetcher 使用，见 docs/plan/p1-doc-pipeline.md）
    fetch_concurrency: int = 4
    request_timeout: float = 30.0
    fetch_max_retries: int = 3
    crawl_delay_default: float = 1.0
    robots_cache_ttl_days: float = 7.0
    user_agent: str = "libref-crawler/0.1"
    # 向量索引（Qdrant，docs/plan/qdrant-migration.md）
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "libref_chunks"
    qdrant_timeout: float = 5.0
    # P3 embedding 参数占位（store/embeddings 使用）。
    # 决策 2026-11：主后端 SiliconFlow 免费 BAAI/bge-m3（OpenAI 兼容 /v1/embeddings），
    # 接口抽象预留多后端（本地 llama-server 等同协议端点，见 tools/hurl/）。
    # key 经 .env 注入（LIBREF_EMBEDDING_API_KEY，勿提交）。
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    embedding_api_base: str = "https://api.siliconflow.cn/v1"
    embedding_api_key: str | None = None  # 仅经 .env / 环境注入；缺省时 embeddings 层禁用向量路
