"""App config: environment variables and sectioned file as two sources; secrets only via environment variables.

Load priority: explicit CLI overrides beat environment variables (including local file injection), environment variables beat the config file, and the config file beats built-in defaults. The effective snapshot is frozen once generated, read-only.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    """Config load failure; the message names the source file or environment variable."""


class LibrefSettings(BaseSettings):
    """Global settings; shared by exit and services layers, the db layer does not depend on this module."""

    model_config = SettingsConfigDict(
        env_prefix="LIBREF_", env_file=".env", frozen=True
    )

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
    # Scrape-path proxy: explicit values beat standard env vars; httpx default env detection applies when unset.
    # Proxy bypass defaults include local loopback; explicit values beat the NO_PROXY env var.
    http_proxy: str | None = None
    no_proxy: str | None = None
    qdrant_url: str = "http://localhost:6333"
    vector_collection: str = "libref_chunks"
    qdrant_timeout: float = Field(default=5.0, gt=0)
    vector_backend: Literal["qdrant", "simvec"] = "simvec"
    simvec_dir: Path | None = None
    simvec_lib: str | None = None
    chunk_embed_max_tokens: int = Field(default=512, ge=1)
    chunk_embed_overlap_tokens: int = Field(default=50, ge=0)
    chunk_embed_min_tokens: int = Field(default=150, ge=0)
    chunk_bm25_max_terms: int = Field(default=150, ge=1)
    chunk_bm25_overlap_terms: int = Field(default=5, ge=0)
    chunk_bm25_min_terms: int = Field(default=45, ge=0)
    chunk_reserve_ratio: float = Field(default=0.06, ge=0)
    # Embedding model name and dim are registered in the DB; on change the vector index is invalidated and rebuilt.
    # Without an API key the vector path is disabled, full-text search only.
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
    retrieval_max_versions: int = Field(default=8, ge=1)
    snippet_radius: int = Field(default=60, ge=0)
    snippet_cap: int = Field(default=400, ge=1)
    window_preview_cap: int = Field(default=300, ge=1)
    fts_w_content: float = Field(default=1.0, ge=0)
    fts_w_title: float = Field(default=5.0, ge=0)
    fts_w_url: float = Field(default=1.0, ge=0)
    fts_w_path: float = Field(default=3.0, ge=0)
    cluster_threshold: float = Field(default=0.15, ge=0)
    cluster_min_size: int = Field(default=2, ge=1)
    evidence_per_file_limit: int = Field(default=1, ge=1)
    evidence_dedup: bool = True
    induction_enabled: bool = False
    induction_topn: int = Field(default=20, ge=1)
    induction_batch: int = Field(default=8, ge=1)
    induction_prompt_version: str = "v1"
    # Registry resolution config: cache TTL, offline toggle, private allow, mirror presets, ecosystem base mapping, external overrides file.
    # Secret-based auth is out of scope; mirror addresses are usable only after being explicitly enabled here.
    # When adding an ecosystem, add entries to the default base and each preset plus a new registry_<eco>.py provider.
    registry_cache_ttl_days: float = Field(default=1.0, ge=0)
    registry_offline: bool = False
    registry_allow_private: bool = False
    # Mirror preset list, see crawler.registry.REGISTRY_MIRRORS; explicit registry_bases wins per key.
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
    # Library discovery config: fuzzy name search across registries; ranking is deterministic, no model involved.
    # Auth tokens travel via environment variables only, never via files.
    discovery_enabled: bool = True
    discovery_topn: int = Field(default=10, ge=1, le=100)
    discovery_cache_ttl_days: float = Field(default=1.0, ge=0)
    discovery_offline: bool = False
    discovery_github_token: str | None = None
    chat_model: str = "Qwen/Qwen3.5-4B"
    chat_enabled: bool = False
    chat_api_base: str = "https://api.siliconflow.cn/v1"
    chat_api_key: str | None = None
    chat_timeout: float = Field(default=60.0, gt=0)
    chat_enable_thinking: bool = False
    material_limit: int = Field(default=1500, ge=1)
    # MCP Streamable HTTP transport: loopback-only by default; exposing externally requires a token.
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8000, ge=1, le=65535)
    mcp_path: str = "/mcp"
    # API auth: token only via env var, never files; a token must be set before external exposure.
    mcp_token: str | None = None
    mcp_require_auth: bool = False
    # Web exit: loopback-only by default; external exposure requires fronting auth.
    web_host: str = "127.0.0.1"
    web_port: int = Field(default=8001, ge=1, le=65535)
    # Docs-path extras: browser fallback default toggle, retrieval window expansion.
    render_js_default: bool = False
    render_wait_for: str | None = None
    render_timeout_ms: int = Field(default=15000, gt=0)
    seeder_cache_ttl_hours: float = Field(default=0.0, ge=0)
    follow_links_default: bool = False
    retriever_window_radius: int = Field(default=1, ge=0)
    retriever_expand_window: bool = False
    # Scrape audit and local governance: snapshot toggle/dir quota, repos dir quota, deletion-log retention.
    # The three paths are independent; each defaults to an absolute path under the home directory, independent of the startup directory.
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
    # Runtime logging: level and file sink; stderr-only and quiet by default.
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
        if str(self.log_level or "").upper() not in (
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
        ):
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
            raise ValueError(
                "chunk_embed_min_tokens must not exceed chunk_embed_max_tokens"
            )
        if self.chunk_bm25_min_terms > self.chunk_bm25_max_terms:
            raise ValueError(
                "chunk_bm25_min_terms must not exceed chunk_bm25_max_terms"
            )
        return self


SECRET_FIELDS = frozenset(
    {"embedding_api_key", "chat_api_key", "mcp_token", "discovery_github_token"}
)


def setting_or_default(settings: LibrefSettings | None, name: str):
    """Resolve from the settings-class default when no settings object exists, avoiding hand-written fallback literals in business code."""
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
            "render_wait_for",
            "render_timeout_ms",
            "seeder_cache_ttl_hours",
            "follow_links_default",
            "ingest_clean_cache_cap",
        }
    ),
    "storage": frozenset(
        {
            "qdrant_url",
            "vector_collection",
            "qdrant_timeout",
            "vector_backend",
            "simvec_dir",
            "simvec_lib",
        }
    ),
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
        {
            "embedding_model",
            "embedding_dim",
            "embedding_api_base",
            "embedding_api_key",
            "embed_batch",
        }
    ),
    "rerank": frozenset(
        {
            "rerank_model",
            "rerank_enabled",
            "rerank_candidate_limit",
            "rerank_top_n",
            "rerank_truncate",
        }
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
            "evidence_per_file_limit",
            "evidence_dedup",
            "induction_enabled",
            "induction_topn",
            "induction_batch",
            "induction_prompt_version",
            "retriever_window_radius",
            "retriever_expand_window",
            "candidate_multiplier",
            "assemble_multiplier",
            "assemble_min",
            "retrieval_max_versions",
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
    "discovery": frozenset(
        {
            "discovery_enabled",
            "discovery_topn",
            "discovery_cache_ttl_days",
            "discovery_offline",
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

RESERVED_SECTIONS: frozenset[str] = frozenset()

PATH_FIELDS = frozenset(
    {
        "db_path",
        "registry_overrides_file",
        "snapshot_dir",
        "repos_dir",
        "log_file",
        "simvec_dir",
    }
)

MASKED_VALUE = "***"


def resolve_config_file(explicit: str | Path | None = None) -> Path | None:
    """Locate the config file by explicit arg, current directory, then user config dir; None if not found."""
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
    """Flatten a sectioned document into a field dict; unknown section or key names raise immediately."""
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
                            raise ConfigError(
                                f"unknown config key '{nested}.{sub}' in {path}"
                            )
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
    """Resolve relative paths in the file against the config file's directory; absolute paths stay unchanged."""
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
    """Fields already provided by env vars or local injected files; same-named file fields yield to them."""
    from pydantic_settings.sources import DotEnvSettingsSource, EnvSettingsSource

    fields = set(LibrefSettings.model_fields)
    covered: set[str] = set()
    for source in (
        EnvSettingsSource(LibrefSettings),
        DotEnvSettingsSource(LibrefSettings),
    ):
        for name in source.env_vars:
            if name.startswith("libref_") and name[7:] in fields:
                covered.add(name[7:])
    return covered


def load_settings(
    config: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> LibrefSettings:
    """Unified load entry: locate file, merge three sources, validate, return a frozen snapshot."""
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
        file_values = _resolve_relative_paths(
            _flatten_document(path, document), path.parent
        )
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
    """Redacted effective snapshot: secrets masked when set, paths as strings, directly serializable."""
    dumped = settings.model_dump()
    for key in SECRET_FIELDS:
        if dumped.get(key):
            dumped[key] = MASKED_VALUE
    for key, value in dumped.items():
        if isinstance(value, Path):
            dumped[key] = str(value)
    return dumped


def settings_fingerprint(settings: LibrefSettings) -> str:
    """Digest of the effective snapshot; stable for identical content, differs on any field change."""
    canonical = json.dumps(redacted_dict(settings), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
