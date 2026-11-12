"""Business tool layer: the only call surface for the three exits."""

from libref.services.code.code_ingest import (
    INDEX_REPO_JOB_KIND,
    build_repo_runner,
    call_uid,
    entity_uid,
    index_repo,
    index_repo_by_name,
    index_repo_by_name_async,
    list_repo_dependencies,
    lookup_index_source_cached,
)
from libref.services.code.mining_read import MiningReadService, is_subsequence
from libref.services.code.mining_refresh import (
    MINE_PATTERNS_JOB_KIND,
    build_mine_runner,
    mine_job_params,
    mine_patterns,
    refresh_clusters,
    refresh_feature_tree,
)
from libref.services.code.probe import RepoProbe, probe_repo
from libref.services.docs.answer import answer_query, answer_query_async
from libref.services.docs.answer_materials import (
    SYSTEM_PROMPT,
    apply_total_budget,
    build_citations,
    build_messages,
    filter_citations_by_answer,
    material_block,
    parse_cited_indices,
)
from libref.services.docs.ingest import (
    SCRAPE_JOB_KIND,
    IngestService,
    build_scrape_runner,
    run_scrape_job_sync,
    scrape_job_params,
    start_refresh_job,
    start_scrape_job,
)
from libref.services.docs.pages import PageService
from libref.services.docs.probe import DocProbe, probe_docs, probe_docs_async
from libref.services.docs.search import PRESETS, SearchService, normalize_preset
from libref.services.factory import (
    build_ingest_service,
    build_maintenance_service,
    build_search_service,
)
from libref.services.infra.jobs import JobManager
from libref.services.ops.maintenance import (
    MaintenanceService,
    check_fts,
    embeddable_doc_ids,
    rebuild_fts,
    reconcile_version,
    sqlite_embeddable_count,
    sync_vectors_for_docs,
    version_drifted,
)
from libref.services.ops.retention import (
    REASON_ABSENT_SWEEP,
    REASON_PURGE,
    check_quota,
    list_deletions,
    prune_expired_logs,
    purge_version,
)
from libref.services.ops.review import ReviewService
from libref.services.registry.discovery import DiscoveryService
from libref.services.registry.libraries import LibraryService, mining_version_ids
from libref.services.registry.resolver import (
    infer_ecosystem,
    resolve,
    resolve_package,
    resolve_repo,
)

__all__ = [
    "INDEX_REPO_JOB_KIND",
    "MINE_PATTERNS_JOB_KIND",
    "PRESETS",
    "REASON_ABSENT_SWEEP",
    "REASON_PURGE",
    "SCRAPE_JOB_KIND",
    "SYSTEM_PROMPT",
    "DiscoveryService",
    "DocProbe",
    "IngestService",
    "JobManager",
    "LibraryService",
    "MaintenanceService",
    "MiningReadService",
    "PageService",
    "RepoProbe",
    "ReviewService",
    "SearchService",
    "answer_query",
    "answer_query_async",
    "apply_total_budget",
    "build_citations",
    "build_ingest_service",
    "build_maintenance_service",
    "build_messages",
    "build_mine_runner",
    "build_repo_runner",
    "build_scrape_runner",
    "build_search_service",
    "call_uid",
    "check_fts",
    "check_quota",
    "embeddable_doc_ids",
    "entity_uid",
    "filter_citations_by_answer",
    "index_repo",
    "index_repo_by_name",
    "index_repo_by_name_async",
    "infer_ecosystem",
    "is_subsequence",
    "list_deletions",
    "list_repo_dependencies",
    "lookup_index_source_cached",
    "material_block",
    "mine_job_params",
    "mine_patterns",
    "mining_version_ids",
    "normalize_preset",
    "parse_cited_indices",
    "probe_docs",
    "probe_docs_async",
    "probe_repo",
    "prune_expired_logs",
    "purge_version",
    "rebuild_fts",
    "reconcile_version",
    "refresh_clusters",
    "refresh_feature_tree",
    "resolve",
    "resolve_package",
    "resolve_repo",
    "run_scrape_job_sync",
    "scrape_job_params",
    "sqlite_embeddable_count",
    "start_refresh_job",
    "start_scrape_job",
    "sync_vectors_for_docs",
    "version_drifted",
]
