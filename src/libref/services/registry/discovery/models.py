"""Discovery candidate contract."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DiscoveryCandidate(BaseModel):
    ecosystem: str
    name: str
    version: str = ""
    description: str = ""
    docs_url: str | None = None
    candidates: list[str] = Field(default_factory=list)
    homepage: str | None = None
    repo_url: str | None = None
    popularity: float = 0.0
    updated: str = ""
    exact: bool = False
    rank: int = 0
    source: str = "search"
