"""应用配置：环境变量与分节文件双来源，密钥只走环境变量。

加载优先级为命令行显式覆盖高于环境变量（含本地文件注入），环境变量高于
配置文件，配置文件高于内置默认值。生效快照一经生成即冻结，只读不写。
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    """配置加载失败，消息内注明来源文件或环境变量名。"""


class LibrefSettings(BaseSettings):
    """全局配置；出口层与 services 层共用，db 层不依赖本模块。"""

    model_config = SettingsConfigDict(env_prefix="LIBREF_", env_file=".env", frozen=True)

    db_path: Path = Field(
        default_factory=lambda: Path.home() / ".local/share/libref/libref.db"
    )
    fetch_concurrency: int = Field(default=4, ge=1)
    request_timeout: float = Field(default=30.0, gt=0)
    fetch_max_retries: int = Field(default=3, ge=0)
    crawl_delay_default: float = Field(default=1.0, ge=0)
    robots_cache_ttl_days: float = Field(default=7.0, ge=0)
    ingest_clean_cache_cap: int = Field(default=512, ge=1)
    user_agent: str = "libref-crawler/0.1"
    # 抓取链路代理：显式值优先于标准环境变量，未设置时沿用 httpx 默认环境识别。
    # 旁路缺省含本地回环，显式值优先于 NO_PROXY 环境变量。
    http_proxy: str | None = None
    no_proxy: str | None = None
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "libref_chunks"
    qdrant_timeout: float = Field(default=5.0, gt=0)
    chunk_embed_max_tokens: int = Field(default=512, ge=1)
    chunk_embed_overlap_tokens: int = Field(default=50, ge=0)
    chunk_embed_min_tokens: int = Field(default=150, ge=0)
    chunk_bm25_max_terms: int = Field(default=150, ge=1)
    chunk_bm25_overlap_terms: int = Field(default=5, ge=0)
    chunk_bm25_min_terms: int = Field(default=45, ge=0)
    chunk_reserve_ratio: float = Field(default=0.06, ge=0)
    # 编码模型名与维度登记在库内，变更时向量索引整体失效重建。
    # 密钥缺省时向量路禁用，只走全文检索。
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    embedding_api_base: str = "https://api.siliconflow.cn/v1"
    embedding_api_key: str | None = None
    embed_batch: int = Field(default=64, ge=1)
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_enabled: bool = False
    rerank_candidate_limit: int = Field(default=30, ge=1)
    rerank_top_n: int = Field(default=10, ge=1)
    rerank_truncate: int = Field(default=500, ge=1)
    rrf_k: int = Field(default=20, ge=1)
    rrf_w_fts: float = Field(default=1.0, ge=0)
    rrf_w_vec: float = Field(default=0.5, ge=0)
    freq_alpha: float = Field(default=0.15, ge=0)
    cluster_alpha: float = Field(default=0.05, ge=0)
    candidate_multiplier: int = Field(default=3, ge=1)
    assemble_multiplier: int = Field(default=2, ge=1)
    assemble_min: int = Field(default=10, ge=1)
    snippet_radius: int = Field(default=60, ge=0)
    snippet_cap: int = Field(default=400, ge=1)
    window_preview_cap: int = Field(default=300, ge=1)
    fts_w_content: float = Field(default=1.0, ge=0)
    fts_w_title: float = Field(default=5.0, ge=0)
    fts_w_url: float = Field(default=1.0, ge=0)
    fts_w_path: float = Field(default=3.0, ge=0)
    cluster_threshold: float = Field(default=0.15, ge=0)
    cluster_min_size: int = Field(default=2, ge=1)
    # 注册表解析配置：缓存有效期、离线开关、私有放行、镜像预设、生态基址映射、外部覆盖文件。
    # 密钥型鉴权不在本方案内，镜像地址经此处显式开启后方可使用。
    # 新增生态时在默认基址与各预设追加条目并新增 registry_<eco>.py 提供方。
    registry_cache_ttl_days: float = Field(default=1.0, ge=0)
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
    registry_override_ttl_days: float = Field(default=180.0, ge=0)
    chat_model: str = "Qwen/Qwen3.5-4B"
    chat_enabled: bool = False
    chat_api_base: str = "https://api.siliconflow.cn/v1"
    chat_api_key: str | None = None
    chat_timeout: float = Field(default=60.0, gt=0)
    chat_enable_thinking: bool = False
    material_limit: int = Field(default=1500, ge=1)
    # MCP Streamable HTTP 传输：默认只绑本地回环，对外暴露需配令牌。
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8000, ge=1, le=65535)
    mcp_path: str = "/mcp"
    # 接口鉴权：令牌只走环境变量，不进文件；对外暴露前必须配令牌。
    mcp_token: str | None = None
    mcp_require_auth: bool = False
    # Web 出口：默认只绑本地回环，对外暴露以前置鉴权为前置条件。
    web_host: str = "127.0.0.1"
    web_port: int = Field(default=8001, ge=1, le=65535)
    # 文档链路补齐：浏览器降级默认开关、检索窗口扩展。
    render_js_default: bool = False
    retriever_window_radius: int = Field(default=1, ge=0)
    retriever_expand_window: bool = False
    # 抓取审计与本地治理：快照开关目录配额、仓库目录配额、删除日志保留期。
    # 三路径相互独立，缺省均为家目录下绝对路径，与启动目录无关。
    snapshot_enabled: bool = False
    snapshot_dir: Path = Field(
        default_factory=lambda: Path.home() / ".local/share/libref/snapshots"
    )
    snapshot_quota_mb: int = Field(default=512, ge=0)
    repos_dir: Path = Field(
        default_factory=lambda: Path.home() / ".local/share/libref/repos"
    )
    repos_quota_mb: int = Field(default=2048, ge=0)
    deletion_log_ttl_days: float = Field(default=30.0, ge=0)
    # 运行日志：级别与落盘，默认只走标准错误且保持安静。
    log_level: str = "WARNING"
    log_file: Path | None = None
    log_format: str = "text"

    @model_validator(mode="after")
    def _freeze_paths(self) -> LibrefSettings:
        for key in PATH_FIELDS:
            value = getattr(self, key, None)
            if value is None:
                continue
            candidate = Path(str(value)).expanduser()
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            object.__setattr__(self, key, candidate)
        return self

    @model_validator(mode="after")
    def _check_logging(self) -> LibrefSettings:
        if str(self.log_level or "").upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ValueError("log_level must be one of DEBUG/INFO/WARNING/ERROR")
        if str(self.log_format or "").lower() not in ("text", "json"):
            raise ValueError("log_format must be one of text/json")
        return self

    @model_validator(mode="after")
    def _check_window_bounds(self) -> LibrefSettings:
        if self.snippet_cap <= self.snippet_radius:
            raise ValueError("snippet_cap must exceed snippet_radius")
        if self.rerank_top_n > self.rerank_candidate_limit:
            raise ValueError("rerank_top_n must not exceed rerank_candidate_limit")
        if self.chunk_embed_min_tokens > self.chunk_embed_max_tokens:
            raise ValueError("chunk_embed_min_tokens must not exceed chunk_embed_max_tokens")
        if self.chunk_bm25_min_terms > self.chunk_bm25_max_terms:
            raise ValueError("chunk_bm25_min_terms must not exceed chunk_bm25_max_terms")
        return self


SECRET_FIELDS = frozenset({"embedding_api_key", "chat_api_key", "mcp_token"})


def setting_or_default(settings: LibrefSettings | None, name: str):
    """无配置对象时按配置类默认值求解，避免业务代码手写兜底字面量。"""
    if settings is not None:
        return getattr(settings, name)
    return LibrefSettings.model_fields[name].default

CONFIG_FILE_NAME = "libref.toml"

SECTION_FIELDS: dict[str, frozenset[str]] = {
    "server": frozenset({"db_path"}),
    "crawler": frozenset(
        {
            "fetch_concurrency",
            "request_timeout",
            "fetch_max_retries",
            "crawl_delay_default",
            "robots_cache_ttl_days",
            "user_agent",
            "http_proxy",
            "no_proxy",
            "render_js_default",
            "ingest_clean_cache_cap",
        }
    ),
    "storage": frozenset({"qdrant_url", "qdrant_collection", "qdrant_timeout"}),
    "chunking": frozenset(
        {
            "chunk_embed_max_tokens",
            "chunk_embed_overlap_tokens",
            "chunk_embed_min_tokens",
            "chunk_bm25_max_terms",
            "chunk_bm25_overlap_terms",
            "chunk_bm25_min_terms",
            "chunk_reserve_ratio",
        }
    ),
    "embeddings": frozenset(
        {"embedding_model", "embedding_dim", "embedding_api_base", "embedding_api_key", "embed_batch"}
    ),
    "rerank": frozenset(
        {"rerank_model", "rerank_enabled", "rerank_candidate_limit", "rerank_top_n", "rerank_truncate"}
    ),
    "retrieval": frozenset(
        {
            "rrf_k",
            "rrf_w_fts",
            "rrf_w_vec",
            "freq_alpha",
            "cluster_alpha",
            "cluster_threshold",
            "cluster_min_size",
            "retriever_window_radius",
            "retriever_expand_window",
            "candidate_multiplier",
            "assemble_multiplier",
            "assemble_min",
            "snippet_radius",
            "snippet_cap",
            "window_preview_cap",
            "fts_w_content",
            "fts_w_title",
            "fts_w_url",
            "fts_w_path",
        }
    ),
    "registry": frozenset(
        {
            "registry_cache_ttl_days",
            "registry_offline",
            "registry_allow_private",
            "registry_mirror",
            "registry_bases",
            "registry_overrides_file",
            "registry_override_ttl_days",
        }
    ),
    "chat": frozenset(
        {
            "chat_model",
            "chat_enabled",
            "chat_api_base",
            "chat_api_key",
            "chat_timeout",
            "chat_enable_thinking",
            "material_limit",
        }
    ),
    "mcp": frozenset({"mcp_host", "mcp_port", "mcp_path"}),
    "mcp.auth": frozenset({"mcp_token", "mcp_require_auth"}),
    "web": frozenset({"web_host", "web_port"}),
    "retention": frozenset(
        {
            "snapshot_enabled",
            "snapshot_dir",
            "snapshot_quota_mb",
            "repos_dir",
            "repos_quota_mb",
            "deletion_log_ttl_days",
        }
    ),
    "logging": frozenset({"log_level", "log_file", "log_format"}),
}

RESERVED_SECTIONS = frozenset()

PATH_FIELDS = frozenset({"db_path", "registry_overrides_file", "snapshot_dir", "repos_dir", "log_file"})

MASKED_VALUE = "***"


def resolve_config_file(explicit: str | Path | None = None) -> Path | None:
    """按显式参数、当前目录、用户配置目录的顺序定位配置文件，未找到返回空。"""
    if explicit is not None:
        candidate = Path(explicit)
        if not candidate.is_file():
            raise ConfigError(f"config file not found: {candidate}")
        return candidate
    local = Path.cwd() / CONFIG_FILE_NAME
    if local.is_file():
        return local
    home_config = Path.home() / ".config" / "libref" / CONFIG_FILE_NAME
    if home_config.is_file():
        return home_config
    return None


def _flatten_document(path: Path, document: Mapping[str, Any]) -> dict[str, Any]:
    """把分节文档展平为字段字典，节名与键名未知时直接报错。"""
    fields = set(LibrefSettings.model_fields)
    flat: dict[str, Any] = {}
    for name, value in document.items():
        if isinstance(value, dict) and name in SECTION_FIELDS:
            allowed = SECTION_FIELDS[name]
            for key, item in value.items():
                nested = f"{name}.{key}"
                if isinstance(item, dict) and nested in SECTION_FIELDS:
                    for sub, sub_item in item.items():
                        if sub not in SECTION_FIELDS[nested]:
                            raise ConfigError(f"unknown config key '{nested}.{sub}' in {path}")
                        _reject_secret(path, sub)
                        flat[sub] = sub_item
                    continue
                if key not in allowed:
                    raise ConfigError(f"unknown config key '{name}.{key}' in {path}")
                _reject_secret(path, key)
                flat[key] = item
        elif isinstance(value, dict) and name in RESERVED_SECTIONS:
            if value:
                raise ConfigError(
                    f"config section [{name}] in {path} is reserved and must stay empty"
                )
        elif name in fields:
            _reject_secret(path, name)
            flat[name] = value
        elif isinstance(value, dict):
            raise ConfigError(f"unknown config section [{name}] in {path}")
        else:
            raise ConfigError(f"unknown config key '{name}' in {path}")
    return flat


def _reject_secret(path: Path, key: str) -> None:
    if key in SECRET_FIELDS:
        raise ConfigError(
            f"config key '{key}' in {path} must come from the environment, not the file"
        )


def _resolve_relative_paths(values: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """文件中的相对路径按配置文件所在目录解析，绝对路径保持不动。"""
    resolved = dict(values)
    for key in PATH_FIELDS & set(values):
        value = values[key]
        if value is None:
            continue
        candidate = Path(str(value)).expanduser()
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        resolved[key] = str(candidate)
    return resolved


def _env_covered_fields() -> set[str]:
    """环境变量与本地注入文件已给出的字段，文件同名字段为其让路。"""
    from pydantic_settings.sources import DotEnvSettingsSource, EnvSettingsSource

    fields = set(LibrefSettings.model_fields)
    covered: set[str] = set()
    for source in (EnvSettingsSource(LibrefSettings), DotEnvSettingsSource(LibrefSettings)):
        for name in source.env_vars:
            if name.startswith("libref_") and name[7:] in fields:
                covered.add(name[7:])
    return covered


def load_settings(
    config: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> LibrefSettings:
    """统一加载入口：定位文件、合并三来源、校验并返回冻结快照。"""
    path = resolve_config_file(config)
    file_values: dict[str, Any] = {}
    if path is not None:
        try:
            with open(path, "rb") as handle:
                document = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"config file is not valid TOML: {path}: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"config file is not readable: {path}: {exc}") from exc
        if not isinstance(document, dict):
            raise ConfigError(f"config file must hold tables: {path}")
        file_values = _resolve_relative_paths(_flatten_document(path, document), path.parent)
        covered = _env_covered_fields()
        file_values = {k: v for k, v in file_values.items() if k not in covered}
    merged: dict[str, Any] = dict(file_values)
    for key, value in dict(overrides or {}).items():
        if key not in LibrefSettings.model_fields:
            raise ConfigError(f"unknown config override '{key}'")
        merged[key] = value
    try:
        return LibrefSettings(**merged)
    except ValidationError as exc:
        origin = f"config file {path}" if path is not None else "environment (LIBREF_*)"
        raise ConfigError(f"invalid configuration from {origin}: {exc}") from exc


def redacted_dict(settings: LibrefSettings) -> dict[str, Any]:
    """脱敏生效快照：密钥有值时掩盖，路径转字符串，可直接序列化。"""
    dumped = settings.model_dump()
    for key in SECRET_FIELDS:
        if dumped.get(key):
            dumped[key] = MASKED_VALUE
    for key, value in dumped.items():
        if isinstance(value, Path):
            dumped[key] = str(value)
    return dumped


def settings_fingerprint(settings: LibrefSettings) -> str:
    """生效快照摘要，同一内容稳定，任一字段变化即不同。"""
    canonical = json.dumps(redacted_dict(settings), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
