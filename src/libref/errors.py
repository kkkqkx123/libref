"""One error table and one error value shared by every exit.

A failure is described where it is detected: a stable code from a closed table,
one sentence naming the offending input and what was expected, and optional
follow-ups a caller can act on. Exits adapt that value to their own wire shape
(MCP result, CLI stderr, HTTP body) instead of inventing codes locally, so the
same input yields the same code everywhere.

Business outcomes (an empty result set, a page that was never indexed, a
degraded retrieval) are not errors and never travel through here.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel

ErrorCode = Literal[
    "unauthorized",
    "forbidden",
    "missing_scope",
    "origin_forbidden",
    "not_found",
    "page_not_found",
    "job_not_found",
    "quota_exceeded",
    "review_conflict",
    "invalid_limit",
    "invalid_offset",
    "invalid_scope",
    "invalid_ecosystem",
    "invalid_argument",
    "chat_disabled",
    "upstream_unavailable",
    "unavailable",
    "index_request_conflict",
    "config_refresh_failed",
]

ALL_CODES: tuple[ErrorCode, ...] = (
    "unauthorized",
    "forbidden",
    "missing_scope",
    "origin_forbidden",
    "not_found",
    "page_not_found",
    "job_not_found",
    "quota_exceeded",
    "review_conflict",
    "invalid_limit",
    "invalid_offset",
    "invalid_scope",
    "invalid_ecosystem",
    "invalid_argument",
    "chat_disabled",
    "upstream_unavailable",
    "unavailable",
    "index_request_conflict",
    "config_refresh_failed",
)

# Codes a headless caller (MCP tool, CLI command) may see. The HTTP-only
# variants below collapse onto these so a caller learns the field, not the
# surface that rejected it.
COARSE_CODES: frozenset[str] = frozenset(
    {
        "invalid_argument",
        "unauthorized",
        "forbidden",
        "not_found",
        "quota_exceeded",
        "index_request_conflict",
        "unavailable",
    }
)

_VARIANTS: dict[str, tuple[ErrorCode, str | None]] = {
    "invalid_limit": ("invalid_argument", "limit"),
    "invalid_offset": ("invalid_argument", "offset"),
    "invalid_scope": ("invalid_argument", "scope"),
    "invalid_ecosystem": ("invalid_argument", "ecosystem"),
    "page_not_found": ("not_found", "url"),
    "job_not_found": ("not_found", "job_id"),
    "missing_scope": ("forbidden", None),
    "origin_forbidden": ("forbidden", None),
    "upstream_unavailable": ("unavailable", None),
    "chat_disabled": ("unavailable", None),
    "config_refresh_failed": ("unavailable", None),
    # Service-layer codes that predate the table.
    "invalid": ("invalid_argument", None),
    "conflict": ("index_request_conflict", None),
    "cancelled": ("index_request_conflict", None),
    "failed": ("unavailable", None),
}

HTTP_STATUS: dict[str, int] = {
    "unauthorized": 401,
    "forbidden": 403,
    "missing_scope": 403,
    "origin_forbidden": 403,
    "not_found": 404,
    "page_not_found": 404,
    "job_not_found": 404,
    "quota_exceeded": 429,
    "review_conflict": 409,
    "index_request_conflict": 409,
    "invalid_limit": 400,
    "invalid_offset": 400,
    "invalid_scope": 400,
    "invalid_ecosystem": 400,
    "invalid_argument": 400,
    "chat_disabled": 400,
    "upstream_unavailable": 503,
    "unavailable": 503,
    "config_refresh_failed": 500,
}

# Caller mistakes exit 2 like any other usage error; a failure the caller could
# not have prevented exits 1.
_USAGE_CODES: frozenset[str] = frozenset(
    {
        "invalid_argument",
        "invalid_limit",
        "invalid_offset",
        "invalid_scope",
        "invalid_ecosystem",
        "unauthorized",
        "forbidden",
        "missing_scope",
        "origin_forbidden",
        "config_refresh_failed",
    }
)


def coarse(code: str) -> ErrorCode:
    """Narrow an HTTP-only variant to the code a headless caller understands."""
    if code in COARSE_CODES:
        return code  # type: ignore[return-value]
    return _VARIANTS.get(code, ("unavailable", None))[0]


def coarse_field(code: str) -> str | None:
    """Field that the collapsed variant used to encode in its own name."""
    return _VARIANTS.get(code, (None, None))[1]


def exit_code_for(code: str) -> int:
    """Process exit status for a failure code."""
    return 2 if code in _USAGE_CODES else 1


def status_for(code: str) -> int:
    """HTTP status for a failure code; unknown codes are treated as outages."""
    return HTTP_STATUS.get(code, 503)


def sanitize(message: str, limit: int = 160) -> str:
    """Reduce any exception text to one sentence safe to hand a model."""
    line = " ".join(str(message).split())
    return line if len(line) <= limit else line[: limit - 3] + "..."


class LibrefError(Exception):
    """A failure described once, before it reaches an exit.

    ``message`` is one sentence: what is wrong and what was expected. ``field``
    names the offending input, ``hint`` carries a single corrective sentence,
    and ``next_actions`` follows the resolver's shape so a caller can replay a
    request instead of guessing. ``request_id`` is filled only when a remote
    server supplied one.

    ``str()`` renders the compact wire payload: every exit that hands an
    exception straight to a caller (the MCP tool runner does) then emits the
    same shape as exits that build the payload explicitly.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        field: str | None = None,
        hint: str | None = None,
        next_actions: list[dict[str, Any]] | None = None,
        request_id: str | None = None,
    ) -> None:
        if code not in ALL_CODES:
            raise ValueError(f"unknown error code: {code}")
        self.code: ErrorCode = code
        self.message = sanitize(message)
        self.field = field
        self.hint = sanitize(hint) if hint else None
        self.next_actions = next_actions or None
        self.request_id = request_id or None
        super().__init__(self.message)

    def payload(self) -> dict[str, Any]:
        """Wire payload; absent optional members are omitted, not nulled."""
        body: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.field:
            body["field"] = self.field
        if self.hint:
            body["hint"] = self.hint
        if self.next_actions:
            body["next_actions"] = self.next_actions
        if self.request_id:
            body["request_id"] = self.request_id
        return body

    def to_json(self) -> str:
        """Compact one-line rendering used by every text exit."""
        return json.dumps(self.payload(), separators=(",", ":"), ensure_ascii=False)

    def __str__(self) -> str:
        return self.to_json()

    def __repr__(self) -> str:
        return f"LibrefError({self.code!r}, {self.message!r})"


class ErrorPayload(BaseModel):
    """The documented shape of :meth:`LibrefError.payload`."""

    error: ErrorCode
    message: str
    field: str | None = None
    hint: str | None = None
    next_actions: list[dict[str, Any]] | None = None
    request_id: str | None = None


def invalid_argument(
    message: str, *, field: str | None = None, hint: str | None = None
) -> LibrefError:
    return LibrefError("invalid_argument", message, field=field, hint=hint)


def not_found(message: str, *, field: str | None = None) -> LibrefError:
    return LibrefError("not_found", message, field=field)


def unavailable(message: str, *, hint: str | None = None) -> LibrefError:
    return LibrefError("unavailable", message, hint=hint)


def normalize(exc: BaseException) -> LibrefError | None:
    """Translate a known failure into one value; ``None`` means "not ours".

    Unknown exceptions stay untouched so a real crash keeps its traceback: an
    exit may only format failures it recognises.
    """
    if isinstance(exc, LibrefError):
        return exc

    from libref.config import ConfigError
    from libref.crawler.registry import RegistryError
    from libref.services.ops.index_requests import IndexRequestError

    if isinstance(exc, ConfigError):
        return LibrefError(
            "config_refresh_failed",
            str(exc),
            hint="fix the configuration or the overrides passed on the command line",
        )
    if isinstance(exc, RegistryError):
        return _from_registry(exc)
    if isinstance(exc, IndexRequestError):
        code = coarse(exc.code)
        return LibrefError(
            code, exc.message, field=coarse_field(exc.code), hint=_HINTS.get(code)
        )
    if isinstance(exc, ValueError):
        return invalid_argument(str(exc))
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return unavailable(
            str(exc) or "the dependency did not answer",
            hint="retry later or rerun with a warm cache",
        )
    return _from_httpx(exc)


def _from_httpx(exc: BaseException) -> LibrefError | None:
    """Transport failures are outages, not caller mistakes."""
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a core dependency
        return None
    if not isinstance(exc, httpx.HTTPError):
        return None
    return unavailable(
        str(exc) or "the remote endpoint did not answer",
        hint="retry later; check the base URL and that the server is running",
    )


_HINTS: dict[str, str] = {
    "unauthorized": "supply credentials for this endpoint",
    "forbidden": "ask for the missing scope or use a permitted account",
    "quota_exceeded": "wait for the quota window to reset",
    "unavailable": "retry later or use a cached result",
    "index_request_conflict": "inspect the existing request instead of resubmitting",
}


_REGISTRY_CODES: dict[str, ErrorCode] = {
    "not_found": "not_found",
    "no_docs": "not_found",
    "no_repo": "not_found",
    "retryable": "unavailable",
}


def _from_registry(exc: Any) -> LibrefError:
    from libref.services.registry.resolver import next_actions_for

    kind = str(getattr(exc, "kind", "") or "")
    code = _REGISTRY_CODES.get(kind, "unavailable")
    hint_info = dict(getattr(exc, "hint", None) or {})
    candidates = [str(c) for c in (hint_info.get("candidates") or [])][:5]
    hint = f"candidates: {', '.join(candidates)}" if candidates else _HINTS.get(code)
    return LibrefError(
        code,
        str(exc),
        hint=hint,
        next_actions=next_actions_for(
            hint_info.get("ecosystem"),
            str(hint_info.get("name") or ""),
            "docs",
            kind,
        ),
    )
