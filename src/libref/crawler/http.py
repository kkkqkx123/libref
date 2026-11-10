"""Shared client construction for the fetch chain: the single place where proxies and bypasses are mounted.

Customization point: an explicit httpx proxy drops the environment NO_PROXY
bypass, so the factory merges the bypass mounts itself under an explicit proxy
to guarantee the local loopback never goes through the proxy. Proxy is ignored
under a test transport, matching httpx's native behavior.
"""

from __future__ import annotations

import ipaddress
import os
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
