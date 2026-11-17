"""Web request/response shapes; only parameter validation and result passthrough."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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


class DocProbeRequest(BaseModel):
    start_url: str = Field(min_length=1)
    scope: dict[str, Any] | None = None
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


class HealthResponse(BaseModel):
    ok: bool = True
    fingerprint: str = ""


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
    params: dict[str, Any] = Field(default_factory=dict)


class KeyCreate(BaseModel):
    name: str = Field(min_length=1)
    kind: str = Field(default="human")
    tenant_id: str = Field(default="default", min_length=1)
    roles: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    expires_at: float | None = None


class Envelope(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    degraded: bool = False
    count: int = 0


class SearchResponse(Envelope):
    reasons: list[str] = Field(default_factory=list)
    answerable: bool = True
    answerability: float = 0.0
    suggestions: list[dict[str, Any]] = Field(default_factory=list)
    suggest_error: str | None = None


class AnswerCitation(BaseModel):
    url: str = ""
    title: str | None = None
    id: int = 0
    library: str | None = None
    version: str | None = None
    source_group_id: str = ""
    chunk_id: str = ""
    page_id: int | None = None
    sort_order: int | None = None


class AnswerResponse(BaseModel):
    answer: str = ""
    citations: list[AnswerCitation] = Field(default_factory=list)
    degraded: bool = False
    reasons: list[str] = Field(default_factory=list)
    model: str = ""
    confidence: str = "low"
    cited_ids: list[int] = Field(default_factory=list)


class ReadResponse(BaseModel):
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
    chunk_details: list[dict[str, Any]] = Field(default_factory=list)
    docs: list[dict[str, Any]] = Field(default_factory=list)


class PageRow(BaseModel):
    """One stored page URL under a library version."""

    id: int = 0
    url: str = ""
    title: str | None = None
    chunks: int = 0


class PageList(BaseModel):
    """Page inventory envelope: the reader is URL-addressed, this lists the URLs."""

    library: str = ""
    version: str = ""
    total: int = 0
    limit: int = 0
    offset: int = 0
    items: list[PageRow] = Field(default_factory=list)
