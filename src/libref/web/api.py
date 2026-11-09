"""Web 请求响应形状，只做参数校验与结果透传。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SearchQuery(BaseModel):
    query: str = Field(min_length=1)
    library: str | None = None
    version: str | None = None
    ecosystem: str | None = None
    limit: int = Field(default=8, ge=1, le=100)
    hybrid: bool = True


class ReadPageQuery(BaseModel):
    url: str = Field(min_length=1)
    library: str | None = None
    version: str | None = None
    ecosystem: str | None = None


class ReadWindowQuery(ReadPageQuery):
    sort_order: int
    radius: int = Field(default=1, ge=0, le=20)


class ReadSectionQuery(ReadPageQuery):
    source_group_id: str = Field(min_length=1)


class ScrapeRequest(BaseModel):
    library: str = Field(min_length=1)
    version: str = Field(min_length=1)
    start_url: str = Field(min_length=1)
    ecosystem: str = "custom"
    max_pages: int | None = None


class ReviewAction(BaseModel):
    entity_id: int
    action: Literal["approve", "conflict", "reopen"]


class HealthResponse(BaseModel):
    ok: bool = True
    fingerprint: str = ""


def envelope(items: list[dict[str, Any]], *, degraded: bool = False) -> dict[str, Any]:
    return {"items": items, "degraded": degraded, "count": len(items)}
