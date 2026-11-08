"""应用配置，环境变量前缀为 LIBREF_，密钥经环境注入不入库。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LibrefSettings(BaseSettings):
    """全局配置；出口层与 services 层共用，db 层不依赖本模块。"""

    model_config = SettingsConfigDict(env_prefix="LIBREF_", env_file=".env")

    db_path: Path = Path("data/libref.db")
    fetch_concurrency: int = 4
    request_timeout: float = 30.0
    fetch_max_retries: int = 3
    crawl_delay_default: float = 1.0
    robots_cache_ttl_days: float = 7.0
    user_agent: str = "libref-crawler/0.1"
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "libref_chunks"
    qdrant_timeout: float = 5.0
    # 编码模型名与维度登记在库内，变更时向量索引整体失效重建。
    # 密钥缺省时向量路禁用，只走全文检索。
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    embedding_api_base: str = "https://api.siliconflow.cn/v1"
    embedding_api_key: str | None = None
    # 注册表解析配置：缓存有效期、离线开关、私有放行、生态基址映射、外部覆盖文件。
    # 密钥型鉴权不在本方案内，镜像地址经此处显式开启后方可使用。
    # 新增生态时在 registry_bases 追加默认基址并新增 registry_<eco>.py 提供方。
    registry_cache_ttl_days: float = 1.0
    registry_offline: bool = False
    registry_allow_private: bool = False
    registry_bases: dict[str, str] = Field(
        default_factory=lambda: {
            "pypi": "https://pypi.org/pypi",
            "npm": "https://registry.npmjs.org",
        }
    )
    registry_overrides_file: Path | None = None
    registry_override_ttl_days: float = 180.0
