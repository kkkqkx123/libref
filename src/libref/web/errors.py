"""Stable error codes for HTTP rejections.

The web UI cannot localise an error without a machine-readable identifier, and
HTTP status codes alone are too coarse (400 covers a dozen unrelated causes).
Every rejection keeps its human-readable ``detail`` untouched and gains an
``error`` code plus the request id. Codes are declared at each rejection point;
messages are never parsed to infer one.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from libref.errors import ALL_CODES, coarse
from libref.services.infra.timestamps import iso_utc
from libref.web.api import KnownErrorCode


def known_error_code(value: str) -> KnownErrorCode:
    """Narrow a service-provided code to one the table defines.

    Services name the field inside the message, so an HTTP-only variant is
    collapsed here rather than kept as a second vocabulary.
    """
    code = value if value in ALL_CODES else coarse(value)
    return code if code in ALL_CODES else "unavailable"


def fallback_code(status: int) -> KnownErrorCode:
    """Name framework-raised rejections that never pass through a route."""
    if status == 404:
        return "not_found"
    if status == 405:
        return "invalid_argument"
    return "unavailable"


class ApiRejection(HTTPException):
    """Rejection that carries an explicit stable code."""

    code: KnownErrorCode

    def __init__(self, status_code: int, code: KnownErrorCode, detail: Any) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.code = code


def error_body(
    status: int, detail: Any, request_id: str = "", *, code: KnownErrorCode
) -> dict[str, Any]:
    """Build the response body shared by route rejections and middleware guards."""
    body: dict[str, Any] = {"error": code, "detail": detail}
    if request_id:
        body["request_id"] = request_id
    if status == 429 and isinstance(detail, dict):
        if detail.get("resource") is not None:
            body["resource"] = detail["resource"]
        if detail.get("reset_at") is not None:
            body["reset_at"] = iso_utc(detail["reset_at"])
    return body
