"""Stable error codes for HTTP rejections.

The web UI cannot localise an error without a machine-readable identifier, and
HTTP status codes alone are too coarse (400 covers a dozen unrelated causes).
Every rejection keeps its human-readable ``detail`` untouched and gains an
``error`` code plus the request id, so existing clients keep working while the
UI can branch on something stable.

Codes come from two places: a rejection point that knows its cause raises
``ApiRejection``, and anything else is resolved from the detail text here. The
text lookup stays because service-layer ``ValueError`` messages fan out across
many routes; new code should prefer ``ApiRejection``.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

# Exact (status, detail) matches for messages the UI needs to tell apart.
_EXACT: dict[tuple[int, str], str] = {
    (401, "unauthorized"): "unauthorized",
    (403, "forbidden"): "forbidden",
    (404, "page not found"): "page_not_found",
    (404, "job not found"): "job_not_found",
    (404, "not found"): "not_found",
    (429, "quota exceeded"): "quota_exceeded",
}

# Prefix matches: detail carries parameters after a fixed lead-in.
_PREFIXES: tuple[tuple[int, str, str], ...] = (
    (403, "forbidden: missing ", "missing_scope"),
    (400, "limit must be ", "invalid_limit"),
    (400, "offset must be ", "invalid_offset"),
    (400, "scope must be ", "invalid_scope"),
    (400, "unsupported ecosystem", "invalid_ecosystem"),
    (409, "cannot transition", "review_conflict"),
    (409, "cannot reopen", "review_conflict"),
)


class ApiRejection(HTTPException):
    """Rejection that carries its own code.

    Used where the route can name the cause outright (chat disabled, upstream
    failure) so the code does not depend on how a message happens to be worded.
    """

    code: str

    def __init__(self, status_code: int, code: str, detail: Any) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.code = code


def error_code(status: int, detail: Any) -> str:
    """Resolve a stable code; unknown causes fall back to ``http_<status>``.

    Structured details (the quota payload) carry their own ``error`` lead-in, so
    the text lookup uses that instead of stringifying the whole mapping. A 400
    without a more specific match still means "bad request argument", which is
    what the UI can phrase; the raw detail travels alongside it.
    """
    text = detail if isinstance(detail, str) else ""
    if isinstance(detail, dict):
        inner = detail.get("error")
        if isinstance(inner, str):
            text = inner
    exact = _EXACT.get((status, text))
    if exact is not None:
        return exact
    for expected_status, prefix, code in _PREFIXES:
        if status == expected_status and text.startswith(prefix):
            return code
    if status == 400:
        return "invalid_argument"
    return f"http_{status}"


def error_body(
    status: int, detail: Any, request_id: str = "", code: str = ""
) -> dict[str, Any]:
    """Build the response body shared by route rejections and middleware guards.

    ``code`` lets callers that already know the cause skip the text lookup; the
    middleware does this because its rejection points are fixed.
    """
    body: dict[str, Any] = {
        "error": code or error_code(status, detail),
        "detail": detail,
    }
    if request_id:
        body["request_id"] = request_id
    if status == 429 and isinstance(detail, dict):
        # Quota payload: lift the reset time so clients that only read the
        # envelope still know when the window rolls over.
        if detail.get("resource") is not None:
            body["resource"] = detail["resource"]
        if detail.get("reset_at") is not None:
            body["reset_at"] = detail["reset_at"]
    return body
