"""业务工具层（三出口唯一调用面）。"""

from libref.services.ingest import (
    SCRAPE_JOB_KIND,
    IngestService,
    build_embed_fn,
    build_scrape_runner,
    run_scrape_job_sync,
    scrape_job_params,
    start_scrape_job,
)
from libref.services.jobs import JobManager
from libref.services.resolver import infer_ecosystem, resolve_package
from libref.services.search import SearchService

__all__ = [
    "SCRAPE_JOB_KIND",
    "IngestService",
    "JobManager",
    "SearchService",
    "build_embed_fn",
    "build_scrape_runner",
    "infer_ecosystem",
    "resolve_package",
    "run_scrape_job_sync",
    "scrape_job_params",
    "start_scrape_job",
]
