"""Index request payload models, validation helpers and state constants."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from libref.services.infra.auth import Principal

ACTIVE_STATUSES = frozenset({"pending", "approved", "queued", "running"})

TERMINAL_STATUSES = frozenset({"done", "failed", "rejected", "cancelled"})

INDEX_JOB_KINDS = frozenset({"scrape_docs", "index_repo"})

_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"approved", "rejected", "cancelled"}),
    "approved": frozenset({"queued", "cancelled", "pending"}),
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"done", "failed", "cancelled"}),
}


def _usage_role(value: Any) -> str:
    """Normalize a requested corpus role before it enters a replayable job."""
    from libref.services.code.usage_sources import SOURCE_ROLES

    role = "implementation" if value in (None, "") else str(value)
    if role not in SOURCE_ROLES:
        raise IndexRequestError(f"unsupported source role: {role}")
    return role


def _target_filter(value: Any) -> dict[str, list[str]]:
    """Keep only the target name and prefix rules understood by the indexer."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise IndexRequestError("target_filter must be an object")
    out: dict[str, list[str]] = {}
    for key in ("names", "prefixes"):
        raw = value.get(key) or []
        if not isinstance(raw, (list, tuple)):
            raise IndexRequestError(f"target_filter.{key} must be an array")
        items = [str(item).strip() for item in raw if str(item).strip()]
        if items:
            out[key] = items
    return out


class IndexRequestItem(BaseModel):
    """One request row as exposed through HTTP, MCP, and CLI."""

    id: int
    tenant_id: str
    principal_id: int | None = None
    requester: str = ""
    kind: str
    target: str
    source: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    status: str
    dedupe_key: str = ""
    approved_by: int | None = None
    reject_reason: str = ""
    job_id: int | None = None
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


class IndexRequestError(Exception):
    """Business failure with a stable code for every exit."""

    def __init__(
        self, message: str, *, code: str = "invalid", status: int = 400
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class QuotaDenied(IndexRequestError):
    """Request frequency limit reached for the acting subject."""

    def __init__(self, resource: str, reset_at: str) -> None:
        super().__init__(
            f"quota exceeded: {resource} (reset at {reset_at})",
            code="quota_exceeded",
            status=429,
        )
        self.resource = resource
        self.reset_at = reset_at


def _json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except ValueError, TypeError:
        return default


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return str(value).strip() if value is not None else ""


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError, TypeError:
        raise IndexRequestError(f"{value!r} is not a valid id") from None


def _actor_id(principal: Principal | None) -> int | None:
    value = getattr(principal, "id", None)
    return int(value) if isinstance(value, int) else None


def _actor_name(principal: Principal | None) -> str:
    return str(getattr(principal, "name", "") or "")


def _has(principal: Principal | None, scope: str) -> bool:
    return bool(principal is not None and principal.has(scope))


def _validate_repo_source(source: str, settings: Any) -> str:
    """Accept only explicit HTTP(S) addresses; local paths stay CLI-direct only."""
    from urllib.parse import urlsplit

    from libref.crawler.registry import sanitize_http_url
    from libref.services.infra.tenant import validate_scrape_target

    text = source.strip().removeprefix("git+")
    try:
        scheme = (urlsplit(text).scheme or "").lower()
    except ValueError as exc:
        raise IndexRequestError(f"invalid repository address: {source}") from exc
    if scheme not in ("http", "https"):
        raise IndexRequestError(
            f"unsupported repository scheme: {scheme or '(missing)'}"
        )
    clean = sanitize_http_url(text, allow_private=False)
    if clean is None:
        raise IndexRequestError(f"invalid repository address: {source}")
    try:
        return validate_scrape_target(clean, settings)
    except ValueError as exc:
        raise IndexRequestError(str(exc)) from exc


def _validate_docs_source(start_url: str, settings: Any) -> str:
    from libref.services.infra.tenant import validate_scrape_target

    try:
        return validate_scrape_target(start_url, settings)
    except ValueError as exc:
        raise IndexRequestError(str(exc)) from exc
