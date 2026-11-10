"""Business tool layer: the only call surface for the three exits."""

from libref.services.answer import answer_query, answer_query_async
from libref.services.answer_materials import (
    SYSTEM_PROMPT,
    build_citations,
    build_messages,
    material_block,
)
from libref.services.code_ingest import (
    INDEX_REPO_JOB_KIND,
    build_repo_runner,
    call_uid,
    entity_uid,
    index_repo,
    index_repo_by_name,
    index_repo_by_name_async,
    lookup_index_source_cached,
)
from libref.services.factory import (
    build_ingest_service,
    build_maintenance_service,
    build_search_service,
)
from libref.services.ingest import (
    SCRAPE_JOB_KIND,
    IngestService,
    build_scrape_runner,
    run_scrape_job_sync,
    scrape_job_params,
    start_refresh_job,
    start_scrape_job,
)
from libref.services.jobs import JobManager
from libref.services.libraries import LibraryService, mining_version_ids
from libref.services.maintenance import (
    MaintenanceService,
    check_fts,
    embeddable_doc_ids,
    rebuild_fts,
    reconcile_version,
    sqlite_embeddable_count,
    sync_vectors_for_docs,
    version_drifted,
)
from libref.services.mining_read import MiningReadService, is_subsequence
from libref.services.mining_refresh import (
    MINE_PATTERNS_JOB_KIND,
    build_mine_runner,
    mine_job_params,
    mine_patterns,
    refresh_clusters,
    refresh_feature_tree,
)
from libref.services.pages import PageService
from libref.services.resolver import infer_ecosystem, resolve_package, resolve_repo
from libref.services.retention import (
    REASON_ABSENT_SWEEP,
    REASON_PURGE,
    check_quota,
    list_deletions,
    prune_expired_logs,
    purge_version,
)
from libref.services.review import ReviewService
from libref.services.search import SearchService

__all__ = [
    "INDEX_REPO_JOB_KIND",
    "MINE_PATTERNS_JOB_KIND",
    "REASON_ABSENT_SWEEP",
    "REASON_PURGE",
    "SCRAPE_JOB_KIND",
    "SYSTEM_PROMPT",
    "IngestService",
    "JobManager",
    "LibraryService",
    "MaintenanceService",
    "MiningReadService",
    "PageService",
    "ReviewService",
    "SearchService",
    "answer_query",
    "answer_query_async",
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
    "index_repo",
    "index_repo_by_name",
    "index_repo_by_name_async",
    "infer_ecosystem",
    "is_subsequence",
    "list_deletions",
    "lookup_index_source_cached",
    "material_block",
    "mine_job_params",
    "mine_patterns",
    "mining_version_ids",
    "prune_expired_logs",
    "purge_version",
    "rebuild_fts",
    "reconcile_version",
    "refresh_clusters",
    "refresh_feature_tree",
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
