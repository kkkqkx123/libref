"""Shared client construction for the fetch chain: the single place where proxies and bypasses are mounted.

Customization point: an explicit httpx proxy drops the environment NO_PROXY
bypass, so the factory merges the bypass mounts itself under an explicit proxy
to guarantee the local loopback never goes through the proxy. Proxy is ignored
under a test transport, matching httpx's native behavior.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.request import getproxies

import httpx

_DEFAULT_NO_PROXY = "localhost,127.0.0.1,::1"


def _is_ipv4(hostname: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(hostname), ipaddress.IPv4Address)
    except ValueError:
        return False


def _is_ipv6(hostname: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(hostname), ipaddress.IPv6Address)
    except ValueError:
        return False


def proxy_bypass_mounts(no_proxy_text: str | None) -> dict[str, None]:
    """Convert NO_PROXY text into a mounts table following httpx's environment bypass rules."""
    mounts: dict[str, None] = {}
    for raw in (no_proxy_text or "").split(","):
        hostname = raw.strip()
        if not hostname or hostname == "*":
            continue
        if "://" in hostname:
            mounts[hostname] = None
        elif _is_ipv4(hostname):
            mounts[f"all://{hostname}"] = None
        elif _is_ipv6(hostname):
            mounts[f"all://[{hostname}]"] = None
        elif hostname.lower() == "localhost":
            mounts[f"all://{hostname}"] = None
        else:
            mounts[f"all://*{hostname}"] = None
    return mounts


def resolve_proxy_url(settings) -> str | None:
    """Explicit LIBREF_HTTP_PROXY wins; otherwise read the standard environment proxy."""
    explicit = str(settings.http_proxy or "").strip()
    if explicit:
        return explicit
    proxies = getproxies()
    for scheme in ("http", "https", "all"):
        if proxies.get(scheme):
            return str(proxies[scheme])
    return None


def resolve_no_proxy_text(settings) -> str:
    """Explicit LIBREF_NO_PROXY wins; otherwise read the environment; otherwise fall back to loopback defaults."""
    explicit = str(settings.no_proxy or "").strip()
    if explicit:
        return explicit
    env = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    if env.strip():
        return env
    return _DEFAULT_NO_PROXY


def proxy_disabled(no_proxy_text: str) -> bool:
    """A fully wildcard bypass means the proxy is disabled."""
    return any(raw.strip() == "*" for raw in (no_proxy_text or "").split(","))


def build_async_client(
    settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """Build a fetch AsyncClient from settings; callers manage its lifetime with async with."""
    kwargs: dict = {
        "timeout": settings.request_timeout,
        "follow_redirects": True,
        "headers": {"User-Agent": settings.user_agent},
        "http2": True,
    }
    if transport is not None:
        return httpx.AsyncClient(transport=transport, **kwargs)
    proxy_url = resolve_proxy_url(settings)
    if proxy_url is None:
        return httpx.AsyncClient(**kwargs)
    no_proxy_text = resolve_no_proxy_text(settings)
    if proxy_disabled(no_proxy_text):
        return httpx.AsyncClient(**kwargs)
    return httpx.AsyncClient(
        proxy=proxy_url, mounts=proxy_bypass_mounts(no_proxy_text), **kwargs
    )


_RETRYABLE_TRANSPORT = (httpx.HTTPError, OSError, TimeoutError)


def retry_after_seconds(value: str | None) -> float:
    """Parse a Retry-After hint in seconds or as an HTTP date."""
    if value is None:
        return 0.0
    text = value.strip()
    if not text:
        return 0.0
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        deadline = parsedate_to_datetime(text)
    except TypeError, ValueError:
        return 0.0
    if deadline is None:
        return 0.0
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return max(0.0, (deadline - datetime.now(UTC)).total_seconds())


def backoff_delay(attempt: int, *, base: float = 0.05, cap: float = 0.4) -> float:
    """Exponential backoff with a small jitter to avoid synchronized retries."""
    return min(max(base * (2**attempt), 0.0), cap) + random.uniform(0.0, 0.02)


def compute_retry_delay(
    resp: httpx.Response | None, attempt: int, *, base: float = 0.05, cap: float = 0.4
) -> float:
    """Wait hint wins over backoff; the sum stays bounded by the cap plus jitter."""
    seen = retry_after_seconds(
        resp.headers.get("retry-after") if resp is not None else None
    )
    return min(max(seen, base * (2**attempt)), cap) + random.uniform(0.0, 0.02)


def _retry_after_delay(resp: httpx.Response, attempt: int) -> float:
    return compute_retry_delay(resp, attempt, base=0.05, cap=0.4)


async def resilient_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    retries: int = 3,
) -> httpx.Response:
    """Shared retry for registry, discovery, and seeding: retry transport errors and 429/5xx, return the final response otherwise."""
    last_error: str | None = None
    last_resp: httpx.Response | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.get(url, headers=headers)
        except _RETRYABLE_TRANSPORT as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(backoff_delay(attempt, base=0.05, cap=0.4))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            last_resp = resp
            last_error = f"HTTP {resp.status_code}"
            await asyncio.sleep(_retry_after_delay(resp, attempt))
            continue
        return resp
    if last_resp is not None:
        return last_resp
    raise httpx.TransportError(last_error or "request unreachable")
