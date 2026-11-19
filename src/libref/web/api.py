"""Web request/response shapes; only parameter validation and result passthrough."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

JobStatus = Literal["pending", "running", "done", "failed"]
IndexRequestStatus = Literal[
    "pending",
    "approved",
    "queued",
    "running",
    "done",
    "failed",
    "rejected",
    "cancelled",
]
ReviewStatus = Literal["pending", "approved", "conflict"]
FeedbackStatus = Literal["open", "acknowledged", "resolved"]
ConfidenceLevel = Literal["EXTRACTED", "INFERRED", "AMBIGUOUS"]
type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[
    str, JsonValue
]


class SearchQuery(BaseModel):
    query: str = Field(min_length=1)
    library: str | None = None
    version: str | None = None
    ecosystem: str | None = None
    limit: int = Field(default=8, ge=1, le=100)
    preset: Literal["precise", "balanced", "thorough"] = "balanced"
    suggest: bool = False
    include_prerelease: bool = False


class LibrarySearchQuery(BaseModel):
    query: str = Field(min_length=1)
    ecosystem: str | None = None
    limit: int = Field(default=10, ge=1, le=100)
    include_prerelease: bool = False


class AnswerQuery(BaseModel):
    query: str = Field(min_length=1)
    library: str | None = None
    version: str | None = None
    ecosystem: str | None = None
    limit: int = Field(default=5, ge=1, le=50)
    preset: Literal["precise", "balanced", "thorough"] = "balanced"


class ReadQuery(BaseModel):
    url: str = Field(min_length=1)
    mode: Literal["page", "window", "section"] = "page"
    sort_order: int | None = None
    radius: int = Field(default=1, ge=0, le=20)
    source_group_id: str | None = None
    library: str | None = None
    version: str | None = None
    ecosystem: str | None = None


class ApiInsightQuery(BaseModel):
    view: Literal["usage", "related", "args", "common"]
    api: str | None = None
    apis: str | None = None
    library: str | None = None
    version: str | None = None
    ecosystem: str | None = None
    limit: int | None = Field(default=None, ge=1, le=500)


class PatternInsightQuery(BaseModel):
    view: Literal[
        "patterns",
        "evidence",
        "tree",
        "clusters",
        "health",
        "labels",
        "snippets",
    ]
    pattern: str | None = None
    containing: str | None = None
    prefix: str | None = None
    library: str | None = None
    version: str | None = None
    ecosystem: str | None = None
    limit: int | None = Field(default=None, ge=1, le=500)
    per_file_limit: int | None = Field(default=None, ge=1, le=20)
    dedup: bool | None = None


class SymbolInsightQuery(BaseModel):
    view: Literal["detail", "file", "children", "signature"]
    api: str | None = None
    apis: str | None = None
    library: str | None = None
    version: str | None = None
    ecosystem: str | None = None
    limit: int | None = Field(default=None, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class CallInsightQuery(BaseModel):
    view: Literal["callee", "caller", "file"]
    api: str | None = None
    library: str | None = None
    version: str | None = None
    ecosystem: str | None = None
    limit: int | None = Field(default=None, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
    include_ambiguous: bool = False
    resolve_kinds: str | None = None
    min_args: int | None = Field(default=None, ge=0)
    max_args: int | None = Field(default=None, ge=0)


class ImportInsightQuery(BaseModel):
    view: Literal["importers", "imports"]
    api: str | None = None
    library: str | None = None
    version: str | None = None
    ecosystem: str | None = None
    limit: int | None = Field(default=None, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class ReviewQuery(BaseModel):
    action: Literal["list", "approve", "conflict", "reopen"] = "list"
    entity_id: int | None = None
    limit: int = Field(default=50, ge=1, le=500)


class ReviewAction(BaseModel):
    action: Literal["list", "approve", "conflict", "reopen"]
    entity_id: int | None = None
    limit: int = Field(default=50, ge=1, le=500)


class ResolveQuery(BaseModel):
    ecosystem: str | None = None
    name: str = Field(min_length=1)
    version: str | None = None
    target: Literal["docs", "repo"] = "docs"
    include_prerelease: bool = False


class JobQuery(BaseModel):
    job_id: int | None = None
    limit: int = Field(default=20, ge=1, le=200)
    full: bool = False


class ScrapeRequest(BaseModel):
    library: str = Field(min_length=1)
    version: str = Field(min_length=1)
    start_url: str = Field(min_length=1)
    ecosystem: str = "custom"
    max_pages: int | None = None


class IndexRequestCreate(BaseModel):
    kind: Literal["repo", "docs"]
    library: str = Field(min_length=1)
    version: str = Field(
        default="", description="Defaults to the resolved stable version"
    )
    ecosystem: str | None = None
    source: str | None = None
    start_url: str | None = None
    max_pages: int | None = Field(default=None, ge=1)
    prefilter: bool = False
    scope: dict[str, JsonValue] | None = None
    update: bool = False
    fresh: bool = False
    include_prerelease: bool = False
    subdir: str | None = None
    only_files: list[str] | None = None
    exclude_patterns: list[str] | None = None
    max_files: int | None = Field(default=None, ge=1)


class IndexRequestReject(BaseModel):
    reason: str = Field(min_length=1)


class DocProbeRequest(BaseModel):
    start_url: str = Field(min_length=1)
    scope: dict[str, JsonValue] | None = None
    sample_limit: int | None = Field(default=None, ge=1, le=100)
    fetch_sample_limit: int | None = Field(default=None, ge=0, le=100)
    max_pages: int | None = Field(default=None, ge=1)
    prefilter: bool = False


class RepoProbeRequest(BaseModel):
    source: str = Field(min_length=1)
    subdir: str | None = None
    max_files: int | None = Field(default=None, ge=1)
    sample_limit: int | None = Field(default=None, ge=1, le=100)
    only_files: list[str] | None = None
    exclude_patterns: list[str] | None = None
    tree_depth: int | None = Field(default=None, ge=1, le=10)
    samples_per_dir: int | None = Field(default=None, ge=1, le=50)


class LinkStatsQuery(BaseModel):
    library: str = Field(min_length=1)
    version: str = Field(min_length=1)
    ecosystem: str | None = None
    limit: int | None = Field(default=None, ge=1, le=500)
    url: str | None = None


class ContractModel(BaseModel):
    """Response base: known fields are the wire contract; extras are dropped."""

    model_config = ConfigDict(extra="ignore")


class QuotaErrorDetail(BaseModel):
    error: str
    resource: str
    reset_at: float | str


class ErrorResponse(BaseModel):
    error: str
    detail: str | QuotaErrorDetail
    request_id: str = ""
    resource: str | None = None
    reset_at: float | str | None = None


class ValidationErrorModel(BaseModel):
    loc: list[str | int]
    msg: str
    type: str
    input: JsonValue | None = None
    ctx: dict[str, JsonValue] | None = None


class ApiValidationError(BaseModel):
    detail: list[ValidationErrorModel]


class HealthResponse(ContractModel):
    ok: bool
    fingerprint: str


def envelope(items: list[dict[str, Any]], *, degraded: bool = False) -> dict[str, Any]:
    return {"items": items, "degraded": degraded, "count": len(items)}


class FeedbackCreate(BaseModel):
    entity_type: str = Field(default="call_instance", min_length=1)
    entity_id: int = Field(default=0, ge=0)
    kind: str = Field(default="general", min_length=1)
    body: str = Field(min_length=1)


class FeedbackResolve(BaseModel):
    pass


class JobCreate(BaseModel):
    kind: str = Field(min_length=1)
    params: dict[str, JsonValue] = Field(default_factory=dict)


class KeyCreate(BaseModel):
    name: str = Field(min_length=1)
    kind: str = Field(default="human")
    tenant_id: str = Field(default="default", min_length=1)
    # developer/kb_admin/sys_admin are mutually exclusive on one principal, and
    # every role must be one the caller already holds (platform role exempt).
    roles: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    expires_at: float | None = None


class QuotaPolicyUpsert(BaseModel):
    resource: str = Field(min_length=1)
    limit_value: int = Field(ge=0)
    window_type: Literal["hour", "day", "month"] = "hour"
    # Empty tenant falls back to the caller's writable tenant.
    tenant_id: str = ""
    principal_id: int | None = Field(default=None, ge=1)


class Envelope[ItemT](ContractModel):
    items: list[ItemT]
    degraded: bool
    count: int


class SearchItem(ContractModel):
    id: int
    url: str
    score: float
    snippet: str
    title: str | None
    path: str | None
    library: str | None
    version: str | None
    source_group_id: str
    chunk_id: str
    page_id: int | None
    sort_order: int | None


class SearchResponse(Envelope[SearchItem]):
    reasons: list[str] = Field(default_factory=list)
    answerable: bool = True
    answerability: float = 0.0
    suggestions: list[DiscoveryCandidate] = Field(default_factory=list)
    suggest_error: str | None = None


class AnswerCitation(ContractModel):
    url: str
    title: str | None
    id: int
    library: str | None
    version: str | None
    source_group_id: str
    chunk_id: str
    page_id: int | None
    sort_order: int | None


class AnswerResponse(ContractModel):
    answer: str
    citations: list[AnswerCitation]
    degraded: bool
    reasons: list[str]
    model: str
    confidence: str
    cited_ids: list[int]


class ChunkLocator(ContractModel):
    source_group_id: str = ""
    chunk_id: str = ""
    span_kind: str = "document_range"
    merged_from: list[JsonValue] = Field(default_factory=list)
    overlap_from: JsonValue | None = None
    overlap_text: str = ""
    chunk_index: int = 0
    total_chunks: int = 1
    length: int = 0
    id: int | None = None
    url: str | None = None
    sort_order: int | None = None


class ReadResponse(ContractModel):
    url: str = ""
    content: str = ""
    library: str | None = None
    version: str | None = None
    title: str | None = None
    path: str | None = None
    source_group_id: str | None = None
    center: int | None = None
    radius: int | None = None
    chunks: int = 0
    chunk_details: list[ChunkLocator] = Field(default_factory=list)
    docs: list[ChunkLocator] = Field(default_factory=list)


class PageRow(ContractModel):
    """One stored page URL under a library version."""

    id: int
    url: str
    title: str | None
    chunks: int


class PageList(ContractModel):
    """Page inventory envelope: the reader is URL-addressed, this lists the URLs."""

    library: str
    version: str
    total: int
    limit: int
    offset: int
    items: list[PageRow]


class PrincipalResponse(ContractModel):
    id: int | None
    name: str
    kind: str
    tenant_id: str
    roles: list[str]
    scopes: list[str]


class MetricsResponse(ContractModel):
    uptime_seconds: int
    duration_ms_p50: int | None = None
    duration_ms_p95: int | None = None
    counters: dict[str, int] = Field(default_factory=dict)


class LibraryRow(ContractModel):
    id: int
    ecosystem: str
    name: str
    description: str | None
    homepage: str | None
    docs_url: str | None
    repo_url: str | None
    resolved_from: str | None
    resolved_at: str | None
    versions: int


class VersionRow(ContractModel):
    id: int
    version: str
    registry_confirmed: bool
    prerelease: bool
    created_at: str | None
    ecosystem: str
    pages: int


class LibraryResponse(Envelope[LibraryRow]):
    kind: Literal["libraries"]


class VersionResponse(Envelope[VersionRow]):
    kind: Literal["versions"]


class ReviewItem(ContractModel):
    entity_id: int
    status: ReviewStatus
    updated_at: str | None
    callee: str | None
    caller_file: str | None
    caller: str | None
    confidence: ConfidenceLevel | None
    args: JsonValue
    line_start: int | None
    line_end: int | None


class ReviewResponse(Envelope[ReviewItem]):
    pass


class OkResponse(ContractModel):
    ok: bool


class OkIdResponse(ContractModel):
    id: int
    ok: bool


class IndexRequestItem(ContractModel):
    id: int
    tenant_id: str
    principal_id: int | None
    requester: str
    kind: Literal["repo", "docs"]
    target: str
    source: str
    params: dict[str, JsonValue]
    status: IndexRequestStatus
    dedupe_key: str
    approved_by: int | None
    reject_reason: str
    job_id: int | None
    error: str
    created_at: str
    updated_at: str


class IndexRequestResponse(Envelope[IndexRequestItem]):
    pass


class DiscoveryCandidate(ContractModel):
    ecosystem: str
    name: str
    version: str
    description: str
    docs_url: str | None
    candidates: list[str]
    homepage: str | None
    repo_url: str | None
    popularity: float
    updated: str
    exact: bool
    rank: int
    source: str


class DiscoveryResponse(Envelope[DiscoveryCandidate]):
    pass


class InsightResponse(Envelope[dict[str, JsonValue]]):
    pass


class PrefixGroup(ContractModel):
    prefix: str
    count: int


class SampleFetch(ContractModel):
    url: str
    status: int | None
    content_type: str
    title: str
    error: str
    outlink_total: int
    outlink_external: int
    external_domains: list[str]
    head_truncated: bool


class DocProbeResponse(ContractModel):
    start_url: str
    source: str
    total_discovered: int
    accepted: int
    rejected: int
    truncated: bool
    failed: bool
    cross_site: int
    max_pages: int | None
    budget_exceeded: bool
    follow_links: bool
    follow_expansion_unknown: bool
    prefix_groups: list[PrefixGroup]
    samples: list[str]
    sample_details: list[SampleFetch]
    errors: dict[str, str]


class RepoProbeDir(ContractModel):
    directory: str
    files: int


class RepoProbeSuffix(ContractModel):
    suffix: str
    count: int


class RepoProbeSamples(ContractModel):
    directory: str
    files: list[str]


class ManifestDependency(ContractModel):
    ecosystem: str
    name: str
    req: str


class RepoProbeResponse(ContractModel):
    source: str
    root_kind: str
    total_candidates: int
    effective_files: int
    skipped: int
    oversized: int
    truncated: bool
    source_note: str
    sizes_known: bool
    subdir: str | None
    only_files: list[str] | None
    unmatched_only_files: list[str]
    exclude_patterns: list[str] | None
    tree_depth: int
    dir_tree: list[RepoProbeDir]
    tree_truncated: bool
    samples_per_dir: int | None
    dir_samples: list[RepoProbeSamples]
    suffix_groups: list[RepoProbeSuffix]
    top_dirs: list[RepoProbeDir]
    samples: list[str]
    manifest_deps: list[ManifestDependency]


class JobSummary(ContractModel):
    id: int
    kind: str
    status: JobStatus
    finished_at: str | None
    error: str | None


class JobListResponse(ContractModel):
    jobs: list[JobSummary]


class JobDetail(ContractModel):
    id: int
    kind: str
    status: JobStatus
    error: str | None
    result: JsonValue | None = None
    params: dict[str, JsonValue] = Field(default_factory=dict)
    finished_at: str | None


class ResolutionResponse(ContractModel):
    ecosystem: str
    name: str
    requested_version: str | None
    version: str
    registry_confirmed: bool
    docs_url: str | None
    candidates: list[str]
    homepage: str | None
    repo_url: str | None
    source: str


class DependencyRow(ContractModel):
    ecosystem: str
    name: str
    req: str
    source_file: str


class DependencyResponse(Envelope[DependencyRow]):
    pass


class TopLink(ContractModel):
    url: str
    inlinks: int


class PageLinkStats(ContractModel):
    kind: Literal["page"]
    url: str
    outlinks: int
    outlink_external: int
    inlinks: int
    inlink_sources: list[str]


class VersionLinkSummary(ContractModel):
    library: str
    version: str
    pages_with_edges: int
    edges: int
    internal_edges: int
    external_edges: int
    structure_edges: int


class VersionLinkStats(ContractModel):
    kind: Literal["version"]
    summary: VersionLinkSummary
    items: list[TopLink]


class MiningHealthResponse(ContractModel):
    patterns: int
    total_support: int
    reusability: float
    diversity: float
    clusters: int


class ReadyResponse(ContractModel):
    ok: bool
    checks: dict[str, str]
    fingerprint: str
    generation: int


class FeedbackItem(ContractModel):
    id: int
    tenant_id: str
    principal_id: int | None
    entity_type: str
    entity_id: int
    kind: str
    body: str
    status: FeedbackStatus
    created_at: str
    resolved_by: int | None
    resolved_at: str | None


class FeedbackResponse(Envelope[FeedbackItem]):
    pass


class ApiKeyRow(ContractModel):
    id: int
    principal_id: int | None
    key_prefix: str
    scopes: list[str]
    expires_at: str | None
    revoked_at: str | None
    last_used_at: str | None
    created_at: str | None


class ApiKeyResponse(Envelope[ApiKeyRow]):
    pass


class PrincipalSummary(ContractModel):
    id: int
    tenant_id: str
    kind: str
    name: str
    status: str
    roles: list[str]
    scopes: list[str]
    conflicting: list[list[str]]


class PrincipalListResponse(Envelope[PrincipalSummary]):
    pass


class IssuedKey(ContractModel):
    id: int
    key: str
    prefix: str


class RevokedKeys(ContractModel):
    ok: bool
    revoked: int


class QuotaPolicy(ContractModel):
    tenant_id: str
    principal_id: int | None
    resource: str
    limit_value: int
    window_type: Literal["hour", "day", "month"]
    updated_at: str


class QuotaPolicyResponse(Envelope[QuotaPolicy]):
    pass


class ConfigRefreshResponse(ContractModel):
    ok: bool
    old_fingerprint: str
    fingerprint: str
