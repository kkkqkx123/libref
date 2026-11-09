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
    # 抓取链路代理：显式值优先于标准环境变量，未设置时沿用 httpx 默认环境识别。
    # 旁路缺省含本地回环，显式值优先于 NO_PROXY 环境变量。
    http_proxy: str | None = None
    no_proxy: str | None = None
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "libref_chunks"
    qdrant_timeout: float = 5.0
    chunk_embed_max_tokens: int = 512
    chunk_embed_overlap_tokens: int = 50
    chunk_embed_min_tokens: int = 150
    chunk_bm25_max_terms: int = 150
    chunk_bm25_overlap_terms: int = 5
    chunk_bm25_min_terms: int = 45
    chunk_reserve_ratio: float = 0.06
    # 编码模型名与维度登记在库内，变更时向量索引整体失效重建。
    # 密钥缺省时向量路禁用，只走全文检索。
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    embedding_api_base: str = "https://api.siliconflow.cn/v1"
    embedding_api_key: str | None = None
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_enabled: bool = False
    rerank_candidate_limit: int = 30
    rerank_top_n: int = 10
    rrf_k: int = 20
    rrf_w_fts: float = 1.0
    rrf_w_vec: float = 0.5
    freq_alpha: float = 0.15
    cluster_alpha: float = 0.05
    cluster_threshold: float = 0.15
    cluster_min_size: int = 2
    # 注册表解析配置：缓存有效期、离线开关、私有放行、镜像预设、生态基址映射、外部覆盖文件。
    # 密钥型鉴权不在本方案内，镜像地址经此处显式开启后方可使用。
    # 新增生态时在默认基址与各预设追加条目并新增 registry_<eco>.py 提供方。
    registry_cache_ttl_days: float = 1.0
    registry_offline: bool = False
    registry_allow_private: bool = False
    # 镜像预设名单，见 crawler.registry.REGISTRY_MIRRORS；显式 registry_bases 逐键优先。
    registry_mirror: str = "default"
    registry_bases: dict[str, str] = Field(
        default_factory=lambda: {
            "pypi": "https://pypi.org/pypi",
            "npm": "https://registry.npmjs.org",
            "maven": "https://repo1.maven.org/maven2",
        }
    )
    registry_overrides_file: Path | None = None
    registry_override_ttl_days: float = 180.0
    chat_model: str = "Qwen/Qwen3.5-4B"
    chat_enabled: bool = False
    chat_api_base: str = "https://api.siliconflow.cn/v1"
    chat_api_key: str | None = None
    chat_timeout: float = 60.0
    chat_enable_thinking: bool = False
    # MCP Streamable HTTP 传输：默认只绑本地回环，无鉴权不对外暴露。
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    mcp_path: str = "/mcp"
    # 文档链路补齐：浏览器降级默认开关、检索窗口扩展。
    render_js_default: bool = False
    retriever_window_radius: int = 1
    retriever_expand_window: bool = False
