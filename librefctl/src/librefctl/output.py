"""One machine-readable output envelope for the client exit."""

from __future__ import annotations

import json
from typing import Any

from librefctl.transport import USAGE_CODES, ClientError


def emit(payload: dict[str, Any], pretty: bool = False) -> None:
    indent = 2 if pretty else None
    print(json.dumps(payload, ensure_ascii=False, indent=indent))


def success(data: Any, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": True, "data": data, "meta": meta or {}}


def failure(error: ClientError) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.field:
        payload["field"] = error.field
    if error.request_id:
        payload["request_id"] = error.request_id
    return {"ok": False, "error": payload}


def exit_code(code: str) -> int:
    return 2 if code in USAGE_CODES else 1
