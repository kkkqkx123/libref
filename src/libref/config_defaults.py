"""Built-in configuration defaults and field groupings."""

from __future__ import annotations

SECRET_FIELDS = frozenset(
    {
        "embedding_api_key",
        "chat_api_key",
        "discovery_github_token",
        "bootstrap_admin_key",
        "postgres_url",
        "qdrant_api_key",
    }
)

CONFIG_FILE_NAME = "libref.toml"

SECTION_FIELDS: dict[str, frozenset[str]] = {
    "server": frozenset(
        {"db_path", "heartbeat_timeout", "instance_id", "deployment_id"}
    ),
    "crawler": frozenset(
        {
            "fetch_concurrency",
            "request_timeout",
            "fetch_max_retries",
            "fetch_max_bytes",
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
            "ingest_batch_pages",
            "index_batch_files",
            "usage_mining_enabled",
            "doc_code_mining_enabled",
            "snippet_languages",
            "snippet_language_rules",
            "stdlib_filter_enabled",
            "stdlib_table_version",
            "probe_sample_limit",
            "probe_cache_ttl_hours",
            "probe_fetch_sample_limit",
            "probe_fetch_timeout",
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
            "vector_distance",
            "vector_write_batch",
            "qdrant_api_key",
            "qdrant_https",
            "qdrant_search_hnsw_ef",
            "qdrant_search_exact",
            "simvec_index",
            "simvec_hnsw_m",
            "simvec_hnsw_ef_construct",
            "simvec_full_scan_threshold",
            "simvec_hnsw_ef_search",
            "simvec_max_indexing_threads",
            "vector_max_points",
            "qdrant_grpc",
            "qdrant_grpc_port",
            "qdrant_prefix",
            "qdrant_pool_size",
            "qdrant_on_disk_vectors",
            "qdrant_on_disk_payload",
            "qdrant_shard_number",
            "qdrant_replication_factor",
            "qdrant_write_consistency_factor",
            "qdrant_hnsw_m",
            "qdrant_hnsw_ef_construct",
            "qdrant_full_scan_threshold",
            "qdrant_memmap_threshold_kb",
            "qdrant_indexing_threshold_kb",
            "metadata_backend",
            "fulltext_backend",
            "postgres_url",
            "postgres_pool_size",
            "postgres_timeout",
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
            "facet_alpha",
            "doc_feature_annotation_top_n",
            "graph_max_depth",
            "graph_max_nodes",
            "graph_max_fanout",
            "graph_pattern_limit",
            "graph_query_budget",
            "cluster_threshold",
            "cluster_min_size",
            "facet_topn",
            "facet_candidate_budget",
            "facet_min_gain",
            "guard_enabled",
            "drift_topn",
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
            "answer_strong_threshold",
            "answer_weak_threshold",
        }
    ),
    "doc_features": frozenset(
        {
            "doc_feature_enabled",
            "doc_feature_candidate_budget",
            "doc_feature_terms_per_facet",
            "doc_feature_min_confidence",
            "doc_feature_enabled_facets",
            "doc_consistency_min_support",
            "doc_consistency_llm_enabled",
            "doc_consistency_batch",
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
            "answer_total_budget",
        }
    ),
    "mcp": frozenset(
        {
            "mcp_path",
            "mcp_enabled_tools",
            "mcp_disabled_tools",
        }
    ),
    "auth": frozenset(
        {
            "auth_mode",
            "dev_principal",
            "allowed_hosts",
            "allowed_origins",
            "bootstrap_admin_key",
            "api_key_default_ttl_hours",
            "api_key_max_ttl_hours",
        }
    ),
    "quota": frozenset(
        {
            "quota_enabled",
            "quota_http_requests_per_hour",
            "quota_llm_tokens_per_day",
            "quota_job_submissions_per_hour",
            "quota_index_requests_per_hour",
            "quota_external_calls_per_hour",
        }
    ),
    "web": frozenset({"web_host", "web_port", "web_spa_enabled", "web_spa_dir"}),
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
        "web_spa_dir",
    }
)

MASKED_VALUE = "***"

URL_FIELDS = frozenset({"qdrant_url"})
"""URL settings whose embedded credentials must never reach logs or fingerprints."""
