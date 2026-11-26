"""Application settings model and effective-configuration helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from libref.config_defaults import PATH_FIELDS


class ConfigError(Exception):
    """Config load failure; the message names the source file or environment variable."""


def _split_tool_names(value):
    """Normalize a tool allowlist or denylist from file, env, or CLI into lowercase deduped names."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError:
                value = [text]
        else:
            value = text.split(",")
    if not isinstance(value, list):
        value = [value]
    cleaned = [str(item).strip().lower() for item in value if str(item).strip()]
    return list(dict.fromkeys(cleaned))


class LibrefSettings(BaseSettings):
    """Global settings; shared by exit and services layers, the db layer does not depend on this module."""

    model_config = SettingsConfigDict(
        env_prefix="LIBREF_", env_file=".env", frozen=True
    )

    db_path: Path = Field(
        default_factory=lambda: Path.home() / ".local/share/libref/libref.db"
    )
    # Heartbeat and staleness horizon shared by job recovery and version locks.
    heartbeat_timeout: float = Field(default=600.0, gt=0)
    fetch_concurrency: int = Field(default=4, ge=1)
    request_timeout: float = Field(default=30.0, gt=0)
    fetch_max_retries: int = Field(default=3, ge=0)
    fetch_max_bytes: int = Field(default=5242880, ge=1)
    crawl_delay_default: float = Field(default=1.0, ge=0)
    robots_cache_ttl_days: float = Field(default=7.0, ge=0)
    ingest_clean_cache_cap: int = Field(default=512, ge=1)
    ingest_batch_pages: int = Field(default=200, ge=1)
    index_batch_files: int = Field(default=200, ge=1)
    usage_mining_enabled: bool = True
    doc_code_mining_enabled: bool = True
    # Acquisition-time candidates for code blocks without explicit language
    # evidence. Parsing validation decides whether a candidate is usable.
    snippet_languages: Annotated[
        list[str], NoDecode, BeforeValidator(_split_tool_names)
    ] = Field(default_factory=list)
    snippet_language_rules: dict[str, str] = Field(default_factory=dict)
    # Standard-library references are classified during extraction; disabling
    # the filter leaves them as ordinary inferred calls for review.
    stdlib_filter_enabled: bool = True
    stdlib_table_version: str = "1"
    probe_sample_limit: int = Field(default=20, ge=1)
    probe_cache_ttl_hours: float = Field(default=1.0, ge=0)
    # Probe sampling: how many discovered pages get a bounded head fetch for
    # title and content type (0 disables), plus the per-fetch timeout.
    probe_fetch_sample_limit: int = Field(default=5, ge=0)
    probe_fetch_timeout: float = Field(default=10.0, gt=0)
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
    # Vector settings shared by both backends. Distance and index-shape settings
    # are part of the collection profile: changing any of them rebuilds the
    # collection instead of migrating points. Search-time and connection
    # settings never affect the on-disk index shape.
    vector_distance: Literal["cosine", "euclid", "dot", "manhattan"] = "cosine"
    vector_write_batch: int = Field(default=512, ge=1)
    # Remote vector service connection. The API key is a secret and travels
    # through the environment only; HTTPS defaults to on when a key is set.
    qdrant_api_key: str | None = None
    qdrant_https: bool | None = None
    # Remote search-time knobs: ef widens the graph beam, exact forces a full scan.
    qdrant_search_hnsw_ef: int | None = Field(default=None, ge=1)
    qdrant_search_exact: bool = False
    # Local engine collection shape. The local tier defaults to the crate's
    # HNSW-first behaviour: exact scan serves until the live-point count crosses
    # the promotion threshold, then the published graph takes over.
    simvec_index: Literal["hnsw", "flat"] = "hnsw"
    simvec_hnsw_m: int = Field(default=16, ge=2)
    simvec_hnsw_ef_construct: int = Field(default=100, ge=4)
    simvec_full_scan_threshold: int = Field(default=10000, ge=1)
    simvec_hnsw_ef_search: int = Field(default=40, ge=1)
    simvec_max_indexing_threads: int | None = Field(default=None, ge=1)
    # Ceiling on points held by the derived index, shared by both backends. A write
    # that would cross it fails instead of filling the disk; the index is derived,
    # so a refused batch is replayable once capacity is raised.
    vector_max_points: int | None = Field(default=None, ge=1)
    # Remote transport. gRPC multiplexes many small batched calls over one
    # connection; REST stays the default for compatibility with older servers.
    qdrant_grpc: bool = False
    qdrant_grpc_port: int = Field(default=6334, ge=1, le=65535)
    qdrant_prefix: str | None = None
    qdrant_pool_size: int | None = Field(default=None, ge=1)
    # Remote collection shape: fixed at creation time, so any change rebuilds the
    # collection and the points are re-embedded from the source of truth. None
    # keeps the service default, which is what an unconfigured deployment gets.
    qdrant_on_disk_vectors: bool | None = None
    qdrant_on_disk_payload: bool | None = None
    qdrant_shard_number: int | None = Field(default=None, ge=1)
    qdrant_replication_factor: int | None = Field(default=None, ge=1)
    qdrant_write_consistency_factor: int | None = Field(default=None, ge=1)
    # Remote tuning: rewritten on the live collection, so existing points survive.
    # Graph parameters and optimizer thresholds decide when a segment leaves memory.
    qdrant_hnsw_m: int | None = Field(default=None, ge=2)
    qdrant_hnsw_ef_construct: int | None = Field(default=None, ge=4)
    qdrant_full_scan_threshold: int | None = Field(default=None, ge=1)
    qdrant_memmap_threshold_kb: int | None = Field(default=None, ge=1)
    qdrant_indexing_threshold_kb: int | None = Field(default=None, ge=1)
    # Storage backend selection: metadata/relations and full-text. SQLite and
    # FTS5 are the only wired backends; remote branches are scaffolding that
    # the assembly layer rejects until implemented.
    metadata_backend: Literal["sqlite", "postgres"] = "sqlite"
    fulltext_backend: Literal["fts5", "postgres"] = "fts5"
    postgres_url: str | None = None
    postgres_pool_size: int = Field(default=8, ge=1)
    postgres_timeout: float = Field(default=10.0, gt=0)
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
    rrf_w_vec: float = Field(default=1.0, ge=0)
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
    # Evidence sufficiency: at or above the strong bound the material set is reported
    # as answering the query, above the weak bound as weak, below it as unable to
    # answer. Bounds come from the retrieval bench: the weak bound keeps the refusal
    # rate on answerable queries near zero, the strong bound marks a settled answer.
    answer_strong_threshold: float = Field(default=0.8, ge=0, le=1)
    answer_weak_threshold: float = Field(default=0.5, ge=0, le=1)
    cluster_threshold: float = Field(default=0.15, ge=0)
    cluster_min_size: int = Field(default=2, ge=1)
    facet_topn: int = Field(default=50, ge=1)
    facet_candidate_budget: int = Field(default=500, ge=0)
    facet_min_gain: float = Field(default=0.0, ge=0)
    guard_enabled: bool = False
    facet_alpha: float = Field(default=0.05, ge=0)
    doc_feature_alpha: float = Field(default=0.0, ge=0)
    drift_topn: int = Field(default=20, ge=1)
    doc_feature_enabled: bool = True
    doc_feature_candidate_budget: int = Field(default=200, ge=1)
    doc_feature_terms_per_facet: int = Field(default=20, ge=1)
    doc_feature_min_confidence: float = Field(default=0.4, ge=0, le=1)
    doc_feature_enabled_facets: Annotated[
        list[str], NoDecode, BeforeValidator(_split_tool_names)
    ] = Field(
        default_factory=lambda: [
            "block_type",
            "heading_depth",
            "length_bucket",
            "code_ratio",
            "has_table",
            "deprecated",
            "version_marker",
            "example_language",
            "example_symbols",
            "param_names",
            "return_hint",
            "term_candidates",
        ]
    )
    doc_consistency_min_support: int = Field(default=2, ge=1)
    doc_consistency_llm_enabled: bool = False
    doc_consistency_batch: int = Field(default=8, ge=1)
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
            "go": "https://proxy.golang.org",
            "crates": "https://crates.io",
            "nuget": "https://api.nuget.org/v3/registration5-gz-semver2",
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
    answer_total_budget: int = Field(default=6000, ge=1)
    # MCP is mounted on the combined Web listener; mcp_path selects its route.
    mcp_path: str = "/mcp"
    # Tool visibility: allowlist minus denylist; an unset allowlist means all tools.
    mcp_enabled_tools: Annotated[
        list[str] | None, NoDecode, BeforeValidator(_split_tool_names)
    ] = None
    mcp_disabled_tools: Annotated[
        list[str], NoDecode, BeforeValidator(_split_tool_names)
    ] = Field(default_factory=list)
    # Authentication is API-Key only: enforced mode resolves Bearer keys to principals.
    auth_mode: Literal["dev", "enforced"] = "enforced"
    dev_principal: str = "dev-operator"
    bootstrap_admin_key: str | None = None
    api_key_default_ttl_hours: float = Field(default=168.0, gt=0)
    api_key_max_ttl_hours: float = Field(default=8760.0, gt=0)
    allowed_hosts: Annotated[
        list[str] | None, NoDecode, BeforeValidator(_split_tool_names)
    ] = None
    allowed_origins: Annotated[
        list[str] | None, NoDecode, BeforeValidator(_split_tool_names)
    ] = None
    instance_id: str = "libref-1"
    # Deployment identity for shared vector services: hosts that declare the
    # same identifier derive the same collection name. Empty falls back to the
    # instance identifier, keeping embedded single-host naming unchanged.
    deployment_id: str = ""
    # Web exit: loopback-only by default; external exposure requires fronting auth.
    web_host: str = "127.0.0.1"
    web_port: int = Field(default=8001, ge=1, le=65535)
    # Self-hosted SPA: empty directory keeps pure-API mode; missing index warns only.
    web_spa_enabled: bool = True
    web_spa_dir: str = ""
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
    # Per-subject quota governance: DB policies take precedence; these global fields are the
    # final fallback limit when no policy row matches. 0 disables that resource's limit.
    quota_enabled: bool = False
    quota_http_requests_per_hour: int = Field(default=0, ge=0)
    quota_llm_tokens_per_day: int = Field(default=0, ge=0)
    quota_job_submissions_per_hour: int = Field(default=0, ge=0)
    quota_index_requests_per_hour: int = Field(default=0, ge=0)
    quota_external_calls_per_hour: int = Field(default=0, ge=0)
    # Runtime logging: level and file sink; stderr-only and quiet by default.
    log_level: str = "WARNING"
    log_file: Path | None = None
    log_format: str = "text"

    @model_validator(mode="after")
    def _freeze_paths(self) -> LibrefSettings:
        for key in PATH_FIELDS:
            value = getattr(self, key, None)
            if value is None or str(value) == "":
                continue
            candidate = Path(str(value)).expanduser()
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            object.__setattr__(self, key, candidate)
        return self

    @model_validator(mode="after")
    def _check_auth(self) -> LibrefSettings:
        if self.api_key_default_ttl_hours > self.api_key_max_ttl_hours:
            raise ValueError(
                "api_key_default_ttl_hours must not exceed api_key_max_ttl_hours"
            )
        from libref.services.infra.auth import validate_origin_config

        validate_origin_config(self.allowed_origins)
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
        if self.answer_weak_threshold > self.answer_strong_threshold:
            raise ValueError(
                "answer_weak_threshold must not exceed answer_strong_threshold"
            )
        return self

    @model_validator(mode="after")
    def _check_vector(self) -> LibrefSettings:
        """Vector settings are validated here so a bad combination fails at load, not at first write."""
        if self.vector_backend == "qdrant" and not str(self.qdrant_url or "").strip():
            raise ValueError("qdrant_url is required when vector_backend is 'qdrant'")
        if self.simvec_index == "hnsw":
            floor = max(2 * self.simvec_hnsw_m, 4)
            if self.simvec_hnsw_ef_construct < floor:
                raise ValueError(
                    "simvec_hnsw_ef_construct must be at least max(2*simvec_hnsw_m, 4)"
                    f" ({floor}) for the hnsw tier"
                )
        # The same graph invariant holds remotely, but only when both sides are set:
        # a lone value must not be constrained by a default the operator never chose.
        if self.qdrant_hnsw_m is not None and self.qdrant_hnsw_ef_construct is not None:
            floor = max(2 * self.qdrant_hnsw_m, 4)
            if self.qdrant_hnsw_ef_construct < floor:
                raise ValueError(
                    "qdrant_hnsw_ef_construct must be at least max(2*qdrant_hnsw_m, 4)"
                    f" ({floor})"
                )
        if (
            self.qdrant_write_consistency_factor is not None
            and self.qdrant_replication_factor is not None
            and self.qdrant_write_consistency_factor > self.qdrant_replication_factor
        ):
            raise ValueError(
                "qdrant_write_consistency_factor must not exceed qdrant_replication_factor"
            )
        return self


def setting_or_default(settings: LibrefSettings | None, name: str):
    """Resolve from the settings-class default when no settings object exists, avoiding hand-written fallback literals in business code."""
    if settings is not None:
        return getattr(settings, name)
    return LibrefSettings.model_fields[name].default


_LOADER_NAMES = {
    "resolve_config_file",
    "load_settings",
    "redacted_dict",
    "settings_fingerprint",
}


def __getattr__(name: str):
    if name not in _LOADER_NAMES:
        raise AttributeError(name)
    from libref import config_loader

    return getattr(config_loader, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LOADER_NAMES)


__all__ = ["ConfigError", "LibrefSettings", "setting_or_default"]
