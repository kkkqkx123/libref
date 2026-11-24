"""Single HTTP transport and error adapter for the client exit."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

USAGE_CODES = frozenset(
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


@dataclass(frozen=True)
class ClientError(Exception):
    code: str
    message: str
    request_id: str = ""
    field: str | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)


class Client:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = (
            httpx.Client(transport=transport, timeout=timeout)
            if transport is not None
            else httpx.Client(timeout=timeout)
        )

    def close(self) -> None:
        self._client.close()

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            response = self._client.request(
                method, url, params=params, json=payload, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise ClientError("unavailable", _brief(str(exc))) from exc
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code >= 400:
            raise _failure(response.status_code, data)
        if data is None:
            return {}
        if not isinstance(data, dict):
            return {"value": data}
        return data


def _brief(text: str) -> str:
    line = " ".join(str(text).split())
    return line if len(line) <= 160 else line[:157] + "..."


def _failure(status: int, data: Any) -> ClientError:
    if not isinstance(data, dict):
        return ClientError("unavailable", f"the server answered with status {status}")
    detail = data.get("detail")
    if isinstance(detail, list) and detail:
        first = detail[0] if isinstance(detail[0], dict) else {}
        loc = first.get("loc") or []
        field = str(loc[-1]) if loc else None
        message = str(first.get("msg") or "the server rejected the request")
        return ClientError("invalid_argument", _brief(message), field=field)
    code = data.get("error")
    if isinstance(code, str) and code:
        if isinstance(detail, dict):
            nested = detail.get("detail")
            detail = (
                nested if nested is not None else json.dumps(detail, ensure_ascii=False)
            )
        message = str(detail or f"the server rejected the request with status {status}")
        request_id = data.get("request_id")
        return ClientError(
            code,
            _brief(message),
            request_id=str(request_id) if request_id else "",
        )
    return ClientError("unavailable", f"the server answered with status {status}")
